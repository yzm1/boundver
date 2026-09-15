"""Five promises boundver makes about every provider it registers, not about five of them.

Each obligation answered here is quantified over the whole provider surface -
"every raw provider", "for every built-in provider", "no boundver operation" -
and each is currently answered by a test that picks one or two names and checks
them. That is not a smaller version of the same claim. The registry holds
thirteen keys built from nine classes and four aliases, and the interesting
members are exactly the ones a sampling test leaves out: `implicit`, which
behaves like a raw provider without being one; `leaf`, which publishes no
digest at all and so satisfies neither side of a two-way split; and the four
`-raw` aliases, which no test resolves today even though a broken alias would
silently change which provider a config selects.

Thirteen keys are not thirteen implementations, and the subtest counts below
should not be read as though they were. `OpenApiProvider`, `JsonFileProvider`,
`PythonExportsProvider` and `TypeScriptExportsProvider` are empty subclasses of
`PathHashProvider` carrying only a name, and `ImplicitProvider.resolve`
delegates to `PathHashProvider().resolve` whenever paths are declared, which
every fixture here does: nine keys share one `resolve` function and ten produce
one digest. What enumerating buys is not ten independent observations of the
same behaviour but the guarantee that a member added or retargeted tomorrow is
placed by a rule instead of being overlooked, and that the alias keys really do
select the provider they name. The last test in this file pins that arithmetic,
so this paragraph cannot quietly go stale.

So the surface is read at runtime from `boundver.create_registry()`, the same
function `_config`, `_lockfile`, `_output` and `_structural_review` call, and
every expectation below is derived rather than listed. Three of the derivations
are behavioural: the entry-label prefix a provider emits on a document valid for
all of them (`file:` against `canonical:`) decides whether it hashes bytes or
parses them; the type of the content object it hands back (`_ModeAwareBytes`
against plain `bytes`) decides whether a Git mode change rotates its digest,
because `_hash_framed_entries` branches on exactly that attribute; and the
answer `explain_provider_diff` gives for well-formed metadata is the reference
every direct-call shape must reproduce, while the lockfile-facing tests require
malformed metadata to be rejected before dispatch. Two are structural: the v0.10 comparability
classes are read from the module constants that define them, and the call sites
that must hand these functions a registry are enumerated from the source tree
with `ast` - where a literal `registry=None` counts as an omission, because that
is the value the fallback to the process global tests for. Where a derivation
runs out, the residue is named in the test that carries it.

Getting a fixture that every member accepts took some care. One file that is
simultaneously valid JSON and a valid OpenAPI 3.1 document lets all thirteen
resolve with status `ok`, which is what makes the classification step mean
anything; without it a provider that failed to resolve would be filed into no
class and quietly skipped. The witness for "syntactically invalid" is `b"{\n"`
rather than the obvious `b"not json { }"`, because YAML parses the latter into a
plain string and the canonical providers would then fail on document shape
instead of on a parse. And the mode fixture cannot use `os.chmod`: this host
leaves `core.filemode` at `false`, so a chmod changes nothing Git records, and a
test written that way would assert that a digest did not move after nothing
moved.

Covers OBL-HASHING-030, OBL-GIT-SOURCE-028, OBL-GLOBS-023, OBL-GIT-SOURCE-125
and OBL-GIT-SOURCE-126.
"""

from __future__ import annotations

import ast
import inspect
import json
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import boundver
from boundver import providers
from boundver._hashing import _ModeAwareBytes
from boundver._lockfile import _SourceAccessor, generate_lockfile
from boundver._migration_analysis import (
    MAX_ANALYSIS_LABEL_CHARS,
    _boundary_analysis_status,
    _prepare_component_declarations,
    _V010_CANONICAL_BOUNDARY_PROVIDERS,
    _V010_RAW_BOUNDARY_PROVIDERS,
)
from boundver._utils import ConfigError
from boundver.providers import (
    ProviderContext,
    ResolvedBoundary,
    compute_boundary,
    create_registry,
    explain_provider_diff,
)

from tests._parity import run_cli, run_cli_in_process
from tests._scenarios import SOURCE_MODES, Scenario

try:  # PyYAML is an optional extra; only one premise assertion needs it.
    import yaml
except ImportError:  # pragma: no cover - exercised on hosts without the extra
    yaml = None

#: The one selector every component declares. The digest is over the entry
#: label as well as the content, and the label carries this filename, so it has
#: to be the same for every member or the shared-digest premise is meaningless.
#: A `.json` suffix also keeps `_parse_yaml_or_json` on its strict-JSON branch,
#: which is what makes the canonical failures independent of PyYAML.
SELECTOR = "contract.json"

#: Valid JSON and a valid OpenAPI 3.1 document at once, so that every one of
#: the thirteen registered providers resolves it with status "ok". That is the
#: precondition for classifying providers by what they return.
CONTRACT = (
    json.dumps(
        {
            "openapi": "3.1.0",
            "info": {"title": "t", "version": "1.0.0"},
            "paths": {"/ping": {"get": {"responses": {"200": {"description": "ok"}}}}},
        },
        indent=2,
    )
    + "\n"
)

#: Syntactically invalid under both parsers a canonical provider can reach.
INVALID = "{\n"

#: The class a provider that emits no entries at all falls into. Spelled with
#: characters no label prefix can contain, so it cannot collide with one.
NO_ENTRIES = "<no entries>"


def registry_keys() -> List[str]:
    """The provider surface, read fresh from the authority production calls."""
    return sorted(create_registry())


def slug(key: str) -> str:
    """A component name for a provider key. Hyphens are not legal in one."""
    return key.replace("-", "_")


def selectors_for(provider_name: str) -> List[str]:
    """Return the valid boundary declaration for a registered provider."""
    return [] if provider_name == "leaf" else [SELECTOR]


class UnclassifiedProvider(AssertionError):
    """A registered provider none of the derivation rules can place."""


# ---------------------------------------------------------------------------
# OBL-HASHING-030: hashing bytes, or refusing to hash a document you cannot parse
# ---------------------------------------------------------------------------


class _Observation:
    """What one provider returned for one document, under one source mode."""

    def __init__(self, labels, status, digest, errors):
        self.labels = list(labels)
        self.status = status
        self.digest = digest
        self.errors = list(errors)

    def __repr__(self) -> str:  # pragma: no cover - failure messages only
        return (
            f"_Observation(labels={self.labels!r}, status={self.status!r}, "
            f"digest={self.digest!r}, errors={self.errors!r})"
        )


def label_class(labels) -> str:
    """The derived class of a provider: the single prefix of its entry labels.

    `ResolvedBoundary`'s own docstring states that a path-based provider labels
    entries `file:<component-relative-path>`, and `JsonCanonicalProvider`'s says
    the canonical label is `canonical:<filename>` precisely to be distinct from
    it. So the prefix is a documented contract rather than an implementation
    accident, and a provider that mixes prefixes, or invents one, is a provider
    this module has no rule for and must say so.
    """
    prefixes = {label.split(":", 1)[0] for label in labels}
    if not prefixes:
        return NO_ENTRIES
    if len(prefixes) != 1:
        raise UnclassifiedProvider(
            f"entry labels carry more than one prefix: {sorted(prefixes)}"
        )
    return prefixes.pop()


#: What each derived class must do with bytes that do not parse. Keyed by the
#: class, never by a provider name: membership is decided at runtime by
#: `label_class`, so a provider added tomorrow is placed by what it returns.
#:
#: The residue is the third row. The obligation states a two-way split - raw
#: providers hash and succeed, canonical providers refuse - and `leaf` is in
#: neither half: it publishes no boundary digest at all, deliberately, with a
#: carve-out in `compute_boundary` that lets only `LeafProvider` return an
#: empty "ok". Removing this row would need a declared capability on the
#: provider saying "publishes a digest"; there is none, so the class is derived
#: from the empty entry list and the outcome is written here.
#:
#: One caveat that belongs with the row rather than under it: emptiness is a
#: property of the declaration as well as of the provider. Publishing providers
#: in this module declare exactly one path; `leaf` validly declares none and is
#: the only member with no entries. Declare `implicit` with an empty path list
#: and it joins the class while returning ("partial", no digest) instead - a
#: second outcome for the same derived class, exercised by
#: `test_the_no_entries_class_depends_on_the_declaration_too`. So this row reads
#: "no entries, given a declaration that selects a file", not "no entries".
INVALID_BYTES_OUTCOME = {
    "file": ("ok", True),
    "canonical": ("error", False),
    NO_ENTRIES: ("ok", False),
}


def _observe_every_provider(text: str) -> Tuple[Dict[str, Dict[str, _Observation]], set]:
    """Resolve every registered provider against identical committed bytes.

    Returns the observations keyed by source mode and then by registry key,
    together with the set of distinct blobs Git actually stored - the caller
    asserts that set has one member, because thirteen components each get their
    own copy of the file and a fixture bug would feed them different content.
    """
    registry = create_registry()
    keys = sorted(registry)
    observed: Dict[str, Dict[str, _Observation]] = {}
    with Scenario("hashing030") as scene:
        for key in keys:
            scene.component(
                key,
                path=f"c/{slug(key)}",
                provider=key,
                boundary=selectors_for(key),
            )
            scene.file(f"c/{slug(key)}/{SELECTOR}", text)
        scene.commit()
        blobs = {scene.blob(f"c/{slug(key)}/{SELECTOR}") for key in keys}
        for source in SOURCE_MODES:
            per_source: Dict[str, _Observation] = {}
            with _SourceAccessor(scene.root, source) as accessor:
                for key in keys:
                    ctx = ProviderContext(
                        repo_root=scene.root,
                        component_path=f"c/{slug(key)}",
                        boundary_cfg=scene.config["components"][key]["boundary"],
                        source=source,
                        read_file=accessor.read_file,
                        read_file_limited=accessor.read_file_limited,
                        list_files=accessor.list_files,
                    )
                    resolved = registry[key].resolve(ctx)
                    digest, status, errors = compute_boundary(registry[key], ctx)
                    per_source[key] = _Observation(
                        [label for label, _ in resolved.entries],
                        status,
                        digest,
                        errors,
                    )
            observed[source] = per_source
    return observed, blobs


class RawAgainstCanonicalOnUnparsableBytesTests(unittest.TestCase):
    """OBL-HASHING-030: hash the bytes, or publish nothing at all."""

    @classmethod
    def setUpClass(cls):
        cls.valid, cls.valid_blobs = _observe_every_provider(CONTRACT)
        cls.invalid, cls.invalid_blobs = _observe_every_provider(INVALID)
        cls.classes: Dict[str, List[str]] = {}
        for key, observation in sorted(cls.valid["head"].items()):
            cls.classes.setdefault(label_class(observation.labels), []).append(key)

    def test_the_witness_bytes_really_do_not_parse(self):
        """The premise: a document that parses would test document shape instead."""
        with self.assertRaises(ValueError):
            json.loads(INVALID)
        if yaml is not None:
            with self.assertRaises(yaml.YAMLError):
                yaml.safe_load(INVALID)
        # And the trap this witness avoids: the obvious alternative parses.
        self.assertEqual(json.loads('"not json"'), "not json")
        if yaml is not None:
            self.assertEqual(yaml.safe_load("not json { }\n"), "not json { }")

    def test_every_component_was_handed_the_identical_bytes(self):
        """The premise: one committed byte string across all thirteen copies."""
        self.assertEqual(self.valid_blobs, {CONTRACT.encode("utf-8")})
        self.assertEqual(self.invalid_blobs, {INVALID.encode("utf-8")})

    def test_the_shared_document_resolves_for_every_registered_provider(self):
        """The premise for the classification: nobody fails, so nobody is skipped.

        "Nobody fails" is a claim about this fixture and not only about the
        providers: every publishing provider declares one path, which puts
        `implicit` in the same class as the raw hashers; `leaf` declares none
        because paths are invalid for a non-publishing boundary.
        """
        keys = registry_keys()
        for source in SOURCE_MODES:
            for key in keys:
                with self.subTest(source=source, provider=key):
                    observation = self.valid[source][key]
                    self.assertEqual(observation.status, "ok", observation)
                    self.assertEqual(observation.errors, [])

    def test_the_registry_partitions_into_three_derived_classes(self):
        """Every member lands in exactly one class, and no class is empty."""
        keys = registry_keys()
        covered = sorted(key for members in self.classes.values() for key in members)
        self.assertEqual(covered, keys)
        self.assertEqual(sorted(self.classes), sorted(INVALID_BYTES_OUTCOME))
        for name, members in sorted(self.classes.items()):
            with self.subTest(derived_class=name):
                self.assertTrue(members, f"derived class {name!r} is empty")
        # The two the obligation names by hand have to be on the parsing
        # side. Which other members join them is left to the derivation.
        self.assertIn("json-canonical", self.classes["canonical"])
        self.assertIn("openapi-canonical", self.classes["canonical"])

    def test_each_class_publishes_a_distinct_digest_for_the_valid_document(self):
        """The premise for "no digest": on a document they can read, they publish one."""
        raw = {self.valid["head"][key].digest for key in self.classes["file"]}
        self.assertEqual(len(raw), 1, f"raw class disagreed: {raw}")
        self.assertNotIn(None, raw)
        canonical = {
            key: self.valid["head"][key].digest for key in self.classes["canonical"]
        }
        for key, digest in canonical.items():
            with self.subTest(provider=key):
                self.assertIsNotNone(digest)
                self.assertNotIn(digest, raw)
        self.assertEqual(len(set(canonical.values())), len(canonical))

    def test_entry_labels_name_the_one_declared_selector(self):
        """A declaration that selected nothing would make every claim below vacuous."""
        for key in self.classes["file"]:
            with self.subTest(provider=key):
                self.assertEqual(self.valid["head"][key].labels, [f"file:{SELECTOR}"])
        for key in self.classes["canonical"]:
            with self.subTest(provider=key):
                self.assertEqual(
                    self.valid["head"][key].labels, [f"canonical:{SELECTOR}"]
                )

    def test_unparsable_bytes_split_the_registry_along_its_derived_classes(self):
        """The obligation, quantified over the whole surface and all three sources."""
        covered = set()
        for source in SOURCE_MODES:
            for derived_class, members in sorted(self.classes.items()):
                self.assertIn(derived_class, INVALID_BYTES_OUTCOME)
                status, has_digest = INVALID_BYTES_OUTCOME[derived_class]
                for key in members:
                    covered.add(key)
                    with self.subTest(source=source, provider=key):
                        observation = self.invalid[source][key]
                        self.assertEqual(observation.status, status, observation)
                        self.assertEqual(
                            observation.digest is not None, has_digest, observation
                        )
        self.assertEqual(covered, set(registry_keys()))

    def test_the_raw_class_agrees_on_one_digest_over_the_unparsable_bytes(self):
        """The provider's own name is not an input to the boundary hash."""
        for source in SOURCE_MODES:
            with self.subTest(source=source):
                digests = {
                    self.invalid[source][key].digest for key in self.classes["file"]
                }
                self.assertEqual(len(digests), 1, digests)
                self.assertNotIn(None, digests)

    def test_the_canonical_class_reports_the_parse_failure_it_refused_on(self):
        """Not merely absent: each says which document it could not read."""
        for key in self.classes["canonical"]:
            with self.subTest(provider=key):
                errors = self.invalid["head"][key].errors
                self.assertEqual(len(errors), 1, errors)
                self.assertIn(f"JSON parse failed for {SELECTOR}", errors[0])

    def test_the_seven_providers_the_obligation_names_are_on_the_surface(self):
        """It names seven of thirteen; the rest are covered by derivation alone."""
        named = {
            "path-hash",
            "json-file",
            "json-file-raw",
            "openapi",
            "openapi-raw",
            "json-canonical",
            "openapi-canonical",
        }
        self.assertTrue(named.issubset(set(registry_keys())))
        raw_named = named - set(self.classes["canonical"])
        self.assertTrue(raw_named.issubset(set(self.classes["file"])))

    def test_the_raw_aliases_select_the_provider_the_package_declares(self):
        """What an alias key selects, checked against the table that declares it.

        The tempting derivation is a tautology. Reading the target out of the
        instance the alias resolves to - `{key: p.name for key, p in registry
        .items() if p.name != key}` - and then asserting `registry[alias] is
        registry[target]` holds for any target `_ALIASES` could name, including
        a wrong one: `create_registry` binds `reg[alias] = reg[target]`, so the
        alias instance's own `.name` is the target's name by construction.
        Retarget `json-file-raw` at `json-canonical` and that assertion still
        passes while a config asking for raw bytes gets a parsing provider.

        So the target comes from `providers._ALIASES`, which is the declaration
        `create_registry` reads rather than the instance it produced, and the
        facts asserted are the ones a retargeted alias breaks: the key spells
        the raw variant of the provider it names, and it lands in the class that
        hashes bytes and publishes a digest for a document it cannot parse.
        """
        registry = create_registry()
        declared = dict(providers._ALIASES)
        self.assertTrue(declared, "no alias keys declared; the loop is vacuous")
        # The registry's own account of which keys are aliases has to agree
        # with the declaration, or one of the two is stale.
        self.assertEqual(
            set(declared),
            {key for key, provider in registry.items() if provider.name != key},
        )
        for alias, target in sorted(declared.items()):
            with self.subTest(alias=alias):
                self.assertIs(registry[alias], registry[target])
                self.assertEqual(registry[alias].name, target)
                # A `-raw` key promises the raw-hash reading of the provider it
                # is named after; both halves of that are assertable.
                self.assertEqual(alias, f"{target}-raw")
                self.assertIn(alias, self.classes["file"])
                self.assertIn(target, self.classes["file"])
                # And the outcome that promise amounts to: unparsable bytes are
                # hashed rather than refused.
                self.assertIsNotNone(self.invalid["head"][alias].digest)
                self.assertEqual(
                    self.invalid["head"][alias].digest,
                    self.invalid["head"][target].digest,
                )

    def test_a_provider_with_an_unfamiliar_label_prefix_is_not_classified(self):
        """The guard: a new member the rules cannot place has to go red, not quiet.

        This is what makes the loop above a checker rather than a table. A
        provider added to `_BUILTIN_PROVIDER_TYPES` tomorrow is classified by
        the prefix it emits; if that prefix is one this module has no outcome
        for, `assertIn` fails and somebody has to decide what the new class
        promises.
        """
        self.assertNotIn(label_class(["sketch:contract.json"]), INVALID_BYTES_OUTCOME)
        with self.assertRaises(UnclassifiedProvider):
            label_class(["file:a", "canonical:b"])

    def test_the_no_entries_class_depends_on_the_declaration_too(self):
        """The limit of the derivation, exercised rather than asserted away.

        `label_class` reads what a provider returned, and what a provider
        returns depends on what was declared. Every component in this module
        declares one path, so `implicit` resolves like a raw hasher and `leaf`
        is the only member of the empty-entry class - which is why the
        `NO_ENTRIES` row can name a single outcome at all. Declared with no
        paths, `implicit` joins that class and produces a different one:
        status `partial`, still no digest, and an error naming the omission.
        `compute_boundary` has a carve-out for it three lines below the one the
        residue comment names, and nothing else in this module reaches it.
        """
        with Scenario("hashing030b") as scene:
            scene.component("declared", path="c/declared", provider="implicit",
                            boundary=[SELECTOR])
            scene.file(f"c/declared/{SELECTOR}", INVALID)
            scene.component("undeclared", path="c/undeclared", provider="implicit",
                            boundary=[])
            scene.file(f"c/undeclared/{SELECTOR}", INVALID)
            scene.commit()
            observed = {}
            provider = create_registry()["implicit"]
            with _SourceAccessor(scene.root, "head") as accessor:
                for name in ("declared", "undeclared"):
                    ctx = ProviderContext(
                        repo_root=scene.root,
                        component_path=f"c/{name}",
                        boundary_cfg=scene.config["components"][name]["boundary"],
                        source="head",
                        read_file=accessor.read_file,
                        read_file_limited=accessor.read_file_limited,
                        list_files=accessor.list_files,
                    )
                    resolved = provider.resolve(ctx)
                    digest, status, errors = compute_boundary(provider, ctx)
                    observed[name] = _Observation(
                        [label for label, _ in resolved.entries], status, digest, errors
                    )

        # With a path declared, implicit is in the class this module tests it in.
        self.assertEqual(label_class(observed["declared"].labels), "file")
        self.assertEqual(
            (observed["declared"].status, observed["declared"].digest is not None),
            INVALID_BYTES_OUTCOME["file"],
        )
        # With none declared, the same provider lands in NO_ENTRIES and does
        # not produce that class's outcome.
        self.assertEqual(label_class(observed["undeclared"].labels), NO_ENTRIES)
        self.assertEqual(observed["undeclared"].status, "partial")
        self.assertIsNone(observed["undeclared"].digest)
        self.assertEqual(
            observed["undeclared"].errors,
            ["No boundary paths declared for implicit boundary"],
        )
        self.assertNotEqual(
            (observed["undeclared"].status, observed["undeclared"].digest is not None),
            INVALID_BYTES_OUTCOME[NO_ENTRIES],
        )


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-028: which providers bind the recorded Git mode
# ---------------------------------------------------------------------------

#: The transitions the obligation walks, with content bytes held constant.
GIT_MODES = ("100644", "100755", "120000")


def _mode_context(provider_name: str, mode: str) -> ProviderContext:
    """A context whose single file carries *mode* and the same content bytes.

    `read_file_limited` is left unset on purpose: `_read_provider_file` falls
    back to `read_file` and accepts a `_ModeAwareBytes` because its type guard
    admits exactly `bytes` and that subclass.
    """
    path = f"svc/{SELECTOR}"
    payload = _ModeAwareBytes(CONTRACT.encode("utf-8"), mode, "blob")
    files = {path: payload}
    return ProviderContext(
        repo_root=Path("/repo"),
        component_path="svc",
        boundary_cfg={"provider": provider_name, "paths": selectors_for(provider_name)},
        source="working-tree",
        read_file=lambda repo_path: files[repo_path],
        list_files=lambda prefix: sorted(
            candidate
            for candidate in files
            if candidate == prefix or candidate.startswith(prefix.rstrip("/") + "/")
        ),
    )


def binds_git_mode(entries) -> bool:
    """Whether a provider's entries carry a Git mode into the hash frame.

    This is not a proxy for the answer, it is the mechanism: `_hash_framed_entries`
    reads `getattr(content, "git_mode", _SEMANTIC_MODE)` per entry, so an entry
    that is a `_ModeAwareBytes` contributes its real mode and a plain `bytes`
    contributes the constant "semantic". `any` is the right quantifier because
    every entry feeds one digest, so a single mode-carrying entry rotates it.
    """
    return any(type(content) is _ModeAwareBytes for _, content in entries)


class GitModeBindingTests(unittest.TestCase):
    """OBL-GIT-SOURCE-028: raw digests move with the mode, canonical ones do not."""

    @classmethod
    def setUpClass(cls):
        registry = create_registry()
        cls.rows: Dict[str, Dict[str, Any]] = {}
        for key in sorted(registry):
            provider = registry[key]
            digests, statuses, errors, labels, binds = [], set(), [], set(), set()
            for mode in GIT_MODES:
                ctx = _mode_context(key, mode)
                resolved = provider.resolve(ctx)
                binds.add(binds_git_mode(resolved.entries))
                labels.update(label for label, _ in resolved.entries)
                digest, status, failures = compute_boundary(provider, ctx)
                digests.append(digest)
                statuses.add(status)
                errors.extend(failures)
            cls.rows[key] = {
                "digests": digests,
                "statuses": statuses,
                "errors": errors,
                "labels": sorted(labels),
                "binds": True in binds,
            }

    def _bucket(self, name: str) -> List[str]:
        if name == "binding":
            return [k for k, r in sorted(self.rows.items()) if r["binds"]]
        if name == "dropping":
            return [
                k for k, r in sorted(self.rows.items()) if not r["binds"] and r["labels"]
            ]
        return [k for k, r in sorted(self.rows.items()) if not r["labels"]]

    def test_every_provider_resolved_the_mode_carrying_document(self):
        """The premise: an erroring provider returns three equal Nones and would
        read as "correctly mode-dropping"."""
        for key in registry_keys():
            with self.subTest(provider=key):
                self.assertEqual(self.rows[key]["statuses"], {"ok"})
                self.assertEqual(self.rows[key]["errors"], [])

    def test_the_content_bytes_never_moved(self):
        """The premise for the raw half: only the mode is allowed to differ."""
        for mode in GIT_MODES:
            with self.subTest(mode=mode):
                payload = _mode_context("path-hash", mode).read_file(f"svc/{SELECTOR}")
                self.assertEqual(bytes(payload), CONTRACT.encode("utf-8"))
                self.assertEqual(payload.git_mode, mode)

    def test_the_partition_is_complete_and_non_degenerate(self):
        binding, dropping, empty = (
            self._bucket("binding"),
            self._bucket("dropping"),
            self._bucket("no entries"),
        )
        self.assertTrue(binding, "no registered provider binds the Git mode")
        self.assertTrue(dropping, "no registered provider drops the Git mode")
        self.assertEqual(
            sorted(binding + dropping + empty),
            registry_keys(),
            "a registered provider fell into no bucket",
        )
        # The two the obligation names by hand must be on the dropping side.
        self.assertIn("json-canonical", dropping)
        self.assertIn("openapi-canonical", dropping)

    def test_the_label_prefix_agrees_with_the_content_type(self):
        """Two independent derivations of the same split; a disagreement is a bug."""
        for key in self._bucket("binding"):
            with self.subTest(provider=key):
                self.assertEqual(self.rows[key]["labels"], [f"file:{SELECTOR}"])
        for key in self._bucket("dropping"):
            with self.subTest(provider=key):
                self.assertEqual(self.rows[key]["labels"], [f"canonical:{SELECTOR}"])

    def test_each_provider_matches_the_expectation_derived_from_its_own_entries(self):
        """The obligation, over every registry key rather than over two of them."""
        covered = set()
        for key in registry_keys():
            row = self.rows[key]
            covered.add(key)
            with self.subTest(provider=key):
                if not row["labels"]:
                    self.assertEqual(row["digests"], [None, None, None])
                elif row["binds"]:
                    self.assertEqual(len(set(row["digests"])), 3, row["digests"])
                    self.assertNotIn(None, row["digests"])
                else:
                    self.assertEqual(len(set(row["digests"])), 1, row["digests"])
                    self.assertNotIn(None, row["digests"])
        self.assertEqual(covered, set(registry_keys()))

    def test_every_mode_binding_provider_shares_one_digest_per_mode(self):
        """Worth pinning because it is the assertion a reader would get wrong.

        "Three distinct digests" is true per member. It is not true that the
        thirty cells are distinct: the entry label is component-relative, so
        every raw member produces the same digest for a given mode.
        """
        for index, mode in enumerate(GIT_MODES):
            with self.subTest(mode=mode):
                digests = {
                    self.rows[key]["digests"][index] for key in self._bucket("binding")
                }
                self.assertEqual(len(digests), 1, digests)

    def test_git_records_the_modes_and_reproduces_the_in_memory_digests(self):
        """The reality anchor: the portable table is not a weaker proxy for git.

        `commit_index` rather than `commit` is required, because `commit` runs
        `git add --all` and would restage the working tree over the mode this
        sets through the index.
        """
        keys = registry_keys()
        with Scenario("gitmode028") as scene:
            for key in keys:
                scene.component(
                    key,
                    path=f"comp/{slug(key)}",
                    provider=key,
                    boundary=selectors_for(key),
                )
                scene.file(f"comp/{slug(key)}/{SELECTOR}", CONTRACT)
            scene.commit("base")
            paths = {key: f"comp/{slug(key)}/{SELECTOR}" for key in keys}
            oids = {
                key: scene.git("ls-files", "-s", "--", path).split()[1]
                for key, path in paths.items()
            }
            self.assertEqual(
                len(set(oids.values())), 1, "components do not share one blob"
            )
            observed: Dict[str, Dict[str, Optional[str]]] = {}
            for mode in GIT_MODES:
                for key in keys:
                    scene.git(
                        "update-index",
                        "--add",
                        "--cacheinfo",
                        f"{mode},{oids[key]},{paths[key]}",
                    )
                scene.commit_index(f"mode {mode}")
                for key in keys:
                    # The premise: read the mode back rather than trusting the
                    # gesture. `os.chmod` would leave it at 100644 on this host.
                    with self.subTest(mode=mode, provider=key):
                        self.assertEqual(
                            scene.git("ls-files", "-s", "--", paths[key]).split()[0],
                            mode,
                        )
                        self.assertEqual(scene.blob(paths[key]), CONTRACT.encode("utf-8"))
                lock = scene.generate(source="head")
                observed[mode] = {
                    key: lock["components"][key]["fingerprints"]["boundary"]
                    for key in keys
                }
            for key in keys:
                with self.subTest(provider=key):
                    self.assertEqual(
                        [observed[mode][key] for mode in GIT_MODES],
                        self.rows[key]["digests"],
                    )


# ---------------------------------------------------------------------------
# OBL-GLOBS-023: what migrate-lock says each provider's selectors meant in 0.10
# ---------------------------------------------------------------------------

#: The three registry members neither v0.10 constant covers, with the pair each
#: returns. This is the module's one hand-written name-to-expectation mapping,
#: and it is written rather than derived because there is nothing to derive it
#: from: I dumped the public attributes of all thirteen registered instances and
#: `implicit`, `leaf` and `path-hash` are indistinguishable - each exposes
#: exactly explain_diff, name, resolve, validate_config, version. Removing this
#: would need a declared v0.10-comparability attribute on the provider, or a
#: third module constant beside the two below. The partition assertion is what
#: keeps it honest: a fourth unclassified member makes this file red.
V010_RESIDUAL = {
    "implicit": ("compared", None),
    "leaf": (
        "not-applicable",
        "The leaf boundary provider ignored boundary.paths in Boundver 0.10",
    ),
    "path-hash": (
        "legacy-rejected",
        "Boundver 0.10 did not register path-hash as a public boundary provider",
    ),
}


def derived_analysis_pair(name: str, is_glob: bool) -> Tuple[str, Optional[str]]:
    """The (status, detail) pair a provider name must produce, by membership."""
    if name in _V010_RAW_BOUNDARY_PROVIDERS:
        return "compared", None
    if name in _V010_CANONICAL_BOUNDARY_PROVIDERS:
        if not is_glob:
            return "compared", None
        return (
            "legacy-rejected",
            f"Boundver 0.10 rejected glob selectors for the {name} boundary provider",
        )
    if name in V010_RESIDUAL:
        return V010_RESIDUAL[name]
    return (
        "provider-specific",
        f"Selector semantics for boundary provider {name!r} cannot be "
        "inferred from the Boundver 0.10 built-ins",
    )


class MigrationAnalysisClassificationTests(unittest.TestCase):
    """OBL-GLOBS-023: one exact pair per registered provider, glob or literal."""

    def test_the_two_constants_and_the_named_residual_partition_the_registry(self):
        """The guard: a provider added tomorrow lands here and fails loudly.

        Without this, a new built-in would fall through to `provider-specific`
        with a detail claiming its semantics cannot be inferred from the 0.10
        built-ins - a true sentence about a custom provider and a wrong one
        about a provider 0.10 simply never had.
        """
        keys = set(registry_keys())
        self.assertTrue(_V010_RAW_BOUNDARY_PROVIDERS.issubset(keys))
        self.assertTrue(_V010_CANONICAL_BOUNDARY_PROVIDERS.issubset(keys))
        residual = keys - _V010_RAW_BOUNDARY_PROVIDERS - _V010_CANONICAL_BOUNDARY_PROVIDERS
        self.assertEqual(residual, set(V010_RESIDUAL))

    def test_every_registered_provider_gets_its_derived_pair(self):
        covered = set()
        for name in registry_keys():
            for is_glob in (False, True):
                covered.add(name)
                with self.subTest(provider=name, glob=is_glob):
                    self.assertEqual(
                        _boundary_analysis_status(name, is_glob),
                        derived_analysis_pair(name, is_glob),
                    )
        self.assertEqual(covered, set(registry_keys()))

    def test_only_the_canonical_class_answers_differently_for_a_glob(self):
        """The premise for the split: something has to depend on glob-ness."""
        splitting = {
            name
            for name in registry_keys()
            if _boundary_analysis_status(name, False)
            != _boundary_analysis_status(name, True)
        }
        self.assertEqual(splitting, set(_V010_CANONICAL_BOUNDARY_PROVIDERS))

    def test_a_name_off_the_registry_is_provider_specific(self):
        for name in ("not-registered", "custom.thing", "", "OPENAPI", "openapi "):
            for is_glob in (False, True):
                with self.subTest(provider=name, glob=is_glob):
                    self.assertEqual(
                        _boundary_analysis_status(name, is_glob),
                        derived_analysis_pair(name, is_glob),
                    )
                    self.assertEqual(
                        _boundary_analysis_status(name, is_glob)[0], "provider-specific"
                    )

    def test_the_provider_specific_detail_is_bounded(self):
        prepared = _prepare_component_declarations(
            "c",
            {
                "path": "c",
                "boundary": {"provider": "z" * MAX_ANALYSIS_LABEL_CHARS, "paths": [SELECTOR]},
            },
        )
        declaration = prepared[2][0]
        self.assertEqual(declaration[6], "provider-specific")
        self.assertEqual(len(declaration[7]), MAX_ANALYSIS_LABEL_CHARS)
        self.assertTrue(declaration[7].endswith("..."))
        with self.assertRaises(ConfigError) as raised:
            _prepare_component_declarations(
                "c",
                {
                    "path": "c",
                    "boundary": {
                        "provider": "z" * (MAX_ANALYSIS_LABEL_CHARS + 1),
                        "paths": [SELECTOR],
                    },
                },
            )
        self.assertIn(
            f"within the {MAX_ANALYSIS_LABEL_CHARS}-character analysis limit",
            str(raised.exception),
        )

    def test_migrate_lock_explain_reports_the_same_pair_for_every_provider(self):
        """One CLI run carrying the whole registry, boundary and behavior facets."""
        keys = registry_keys()
        literal, glob = "api/v1.yaml", "api/*.yaml"
        with Scenario("globs023") as scene:
            for key in keys:
                name = slug(key)
                scene.component(
                    name,
                    path=f"c/{name}",
                    provider=key,
                    boundary=[literal, glob],
                    behavior=[literal, glob],
                )
                scene.file(f"c/{name}/{literal}", "top\n")
                scene.file(f"c/{name}/api/nested/deep.yaml", "deep\n")
            (scene.root / "old.lock.json").write_text(
                json.dumps(
                    {
                        "schema": "boundary-lock/v2",
                        "project": "globs023",
                        "components": {},
                        "slices": {},
                    }
                ),
                encoding="utf-8",
            )
            scene.commit()
            result = run_cli(
                scene.root,
                "migrate-lock",
                "--lock",
                "old.lock.json",
                "--explain",
                "--config",
                "boundary.config.json",
                "--source",
                "head",
                "--format",
                "json",
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        rows = payload["declarations"]

        # The premise the recorded gap names: a component that contributes no
        # declaration makes every loop over this payload assert nothing.
        self.assertEqual(len(rows), 4 * len(keys))
        self.assertEqual(
            {(row["component"], row["facet"], row["selector_kind"]) for row in rows},
            {
                (slug(key), facet, kind)
                for key in keys
                for facet in ("boundary", "behavior")
                for kind in ("glob", "literal")
            },
        )

        covered = set()
        by_slug = {slug(key): key for key in keys}
        for row in rows:
            key = by_slug[row["component"]]
            covered.add(key)
            with self.subTest(provider=key, facet=row["facet"], kind=row["selector_kind"]):
                if row["facet"] == "behavior":
                    # A behavior declaration has no provider, so the table is
                    # bypassed no matter what the component's boundary declares.
                    self.assertIsNone(row["provider"])
                    self.assertEqual(row["analysis_status"], "compared")
                    self.assertIsNone(row["detail"])
                else:
                    self.assertEqual(row["provider"], key)
                    status, detail = derived_analysis_pair(
                        key, row["selector_kind"] == "glob"
                    )
                    self.assertEqual(row["analysis_status"], status)
                    self.assertEqual(row["detail"], detail)
                counts = (
                    "legacy_match_count",
                    "current_match_count",
                    "legacy_only_count",
                    "current_only_count",
                )
                if row["analysis_status"] == "compared":
                    # The second premise: "compared" over an empty match set
                    # would be a verdict about nothing.
                    for field in counts:
                        self.assertIsInstance(row[field], int)
                    self.assertGreaterEqual(row["legacy_match_count"], 1)
                    if row["selector_kind"] == "glob":
                        self.assertEqual(row["impact"], "narrowed")
                        self.assertEqual(
                            row["legacy_only_examples"], ["api/nested/deep.yaml"]
                        )
                    else:
                        self.assertEqual(row["impact"], "unchanged")
                else:
                    self.assertEqual(row["impact"], "not-comparable")
                    for field in counts:
                        self.assertIsNone(row[field])
        self.assertEqual(covered, set(keys))


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-125: no operation writes the process-global registry
# ---------------------------------------------------------------------------

#: Declared by the isolation fixture's config. Importing this test module by
#: name is what makes an in-process run reach it; a subprocess run would not,
#: and a subprocess run cannot witness this obligation anyway.
CUSTOM_PROVIDER_NAME = "custom.registry-probe"


class RegistryProbeProvider:
    """A custom provider whose boundary digest is a constant.

    Constant on purpose: `verify` must report no drift, so the isolation sweep
    observes the registry rather than a failing component.
    """

    name = CUSTOM_PROVIDER_NAME
    version = "1"

    def resolve(self, ctx):
        return ResolvedBoundary(
            entries=[("file:probe", b"probe-constant")],
            status="ok",
            metadata={"probe": True},
        )

    def validate_config(self, boundary_cfg, component_path, repo_root):
        return []

    def explain_diff(self, old_metadata, new_metadata, ctx):
        return "probe boundary changed"


CUSTOM_PROVIDER_DECLARATION = [
    {"module": __name__, "class": "RegistryProbeProvider", "name": CUSTOM_PROVIDER_NAME}
]


def _registry_snapshot() -> Dict[str, int]:
    """Keys plus instance identities: a rebound name is a mutation too."""
    return {name: id(instance) for name, instance in providers._REGISTRY.items()}


#: Call-site verdicts. "supplied" is the only one the scan accepts.
CALL_SITE_VERDICTS = ("supplied", "missing", "explicit-none", "unknown")


def registry_argument(node: ast.Call, index: int) -> str:
    """How one call site supplies the `registry` argument, by verdict.

    Spelling the keyword is not the property. `get_provider(name,
    registry=None)` names it and still selects the process global, because the
    fallback these functions share is `registry if registry is not None else
    _REGISTRY` - so a literal `None` is the shape the obligation forbids
    wearing the shape it demands, and it gets its own verdict rather than
    counting as supplied. A call that unpacks `*args` or `**kwargs` cannot be
    read statically at all; calling that a violation would be a guess, so it is
    reported as unknown and the caller decides what to do about it.
    """
    if any(keyword.arg is None for keyword in node.keywords) or any(
        isinstance(argument, ast.Starred) for argument in node.args
    ):
        supplied_here = next(
            (keyword.value for keyword in node.keywords if keyword.arg == "registry"),
            None,
        )
        if supplied_here is None:
            return "unknown"
        value: Optional[ast.expr] = supplied_here
    else:
        keyword = next(
            (keyword for keyword in node.keywords if keyword.arg == "registry"), None
        )
        value = (
            keyword.value
            if keyword is not None
            else node.args[index] if len(node.args) > index else None
        )
    if value is None:
        return "missing"
    if isinstance(value, ast.Constant) and value.value is None:
        return "explicit-none"
    return "supplied"


class ProcessGlobalRegistryIsolationTests(unittest.TestCase):
    """OBL-GIT-SOURCE-125: a fresh registry per operation, or one config leaks."""

    def setUp(self):
        self._backup = dict(providers._REGISTRY)

    def tearDown(self):
        providers._REGISTRY.clear()
        providers._REGISTRY.update(self._backup)

    def test_the_cli_under_test_shares_this_interpreter_s_registry(self):
        """The premise: run in a subprocess and the snapshot proves nothing."""
        self.assertIs(sys.modules["boundver.providers"], providers)

    def test_the_snapshot_comparison_can_see_a_real_mutation(self):
        """The negative control: omit `registry=` and the global does change."""
        before = _registry_snapshot()
        errors = providers.load_custom_providers(
            CUSTOM_PROVIDER_DECLARATION, allow_custom=True
        )
        self.assertEqual(errors, [])
        self.assertEqual(
            set(_registry_snapshot()) - set(before), {CUSTOM_PROVIDER_NAME}
        )

    def _fixture(self, scene: Scenario) -> Scenario:
        """Every registry key plus the custom provider, in one config."""
        scene.config["providers"] = list(CUSTOM_PROVIDER_DECLARATION)
        for key in registry_keys() + [CUSTOM_PROVIDER_NAME]:
            name = slug(key.replace(".", "_"))
            scene.component(
                name,
                path=f"c/{name}",
                provider=key,
                boundary=selectors_for(key),
            )
            scene.file(f"c/{name}/{SELECTOR}", CONTRACT)
        return scene

    def test_no_operation_writes_the_global_registry(self):
        """generate, verify, validate-config, review and diff, in one process."""
        allow = "--allow-custom-providers"
        keys = registry_keys() + [CUSTOM_PROVIDER_NAME]
        with self._fixture(Scenario("isolation125")) as scene:
            scene.commit("base")
            baseline = _registry_snapshot()

            self.assertEqual(
                run_cli_in_process(scene.root, "generate", allow).returncode, 0
            )
            self.assertEqual(_registry_snapshot(), baseline, "generate")
            scene.git("add", "--all")
            scene.git("commit", "-m", "lock")
            base_ref = scene.head()

            lock = json.loads(
                (scene.root / "boundary.lock.json").read_text(encoding="utf-8")
            )
            # The premise: every enumerated key really was resolved, under the
            # registry key rather than the aliased provider.name.
            self.assertEqual(
                {
                    name: component["boundary_provider"]
                    for name, component in lock["components"].items()
                },
                {slug(key.replace(".", "_")): key for key in keys},
            )
            self.assertEqual(
                lock["components"][slug(CUSTOM_PROVIDER_NAME.replace(".", "_"))][
                    "boundary_metadata"
                ],
                {"probe": True},
            )

            scene.file(f"c/{slug('path-hash')}/{SELECTOR}", CONTRACT.replace("/ping", "/pong"))
            scene.commit("edit")
            self.assertEqual(
                run_cli_in_process(scene.root, "generate", allow).returncode, 0
            )
            scene.git("add", "--all")
            scene.git("commit", "-m", "lock2")
            target_ref = scene.head()
            (scene.root / "old.lock.json").write_bytes(
                (scene.root / "boundary.lock.json").read_bytes()
            )
            scene.commit("old lock")

            operations = (
                ("verify", allow),
                ("validate-config", allow),
                ("review", f"{base_ref}..{target_ref}", allow),
                ("diff", "old.lock.json", "boundary.lock.json"),
            )
            for command in operations:
                with self.subTest(operation=command[0]):
                    result = run_cli_in_process(scene.root, *command)
                    # Exit 0 is a premise as much as an assertion: an
                    # operation that refused the config never reached the
                    # registry, and its snapshot would prove nothing.
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(_registry_snapshot(), baseline, command[0])

            self.assertEqual(_registry_snapshot(), baseline)
            self.assertNotIn(CUSTOM_PROVIDER_NAME, providers._REGISTRY)

    def test_a_polluted_global_does_not_change_a_later_answer(self):
        """The positive control, and the obligation's second operation.

        A config naming an undeclared custom provider must fail as unknown -
        and must fail the same way after the global has been written by hand,
        which is the only evidence that production reads a fresh registry
        rather than merely declining to write the global one.
        """
        with Scenario("isolation125b") as scene:
            scene.component(
                "svc", path="c/svc", provider=CUSTOM_PROVIDER_NAME, boundary=[SELECTOR]
            )
            scene.file(f"c/svc/{SELECTOR}", CONTRACT)
            scene.commit("base")

            def answer() -> str:
                with self.assertRaises(ConfigError) as raised:
                    generate_lockfile(
                        scene.config,
                        scene.root,
                        source="head",
                        allow_custom_providers=False,
                    )
                return str(raised.exception)

            clean = answer()
            self.assertIn(
                f"Unknown boundary provider: {CUSTOM_PROVIDER_NAME!r}", clean
            )
            providers.register_provider(RegistryProbeProvider())
            self.assertIn(CUSTOM_PROVIDER_NAME, providers._REGISTRY)
            self.assertEqual(answer(), clean)

    @staticmethod
    def _watched_positions() -> Dict[str, int]:
        """Where `registry` sits in each watched signature, read from it."""
        watched = {
            "register_provider": providers.register_provider,
            "get_provider": providers.get_provider,
            "load_custom_providers": providers.load_custom_providers,
        }
        return {
            name: list(inspect.signature(function).parameters).index("registry")
            for name, function in watched.items()
        }

    def test_the_call_site_check_can_tell_the_forbidden_shapes_apart(self):
        """The control for the scan below: its predicate has to discriminate.

        A check that only asked whether the keyword appears would pass on
        `get_provider(name, registry=None)`, which is precisely the
        fallback-to-the-global call it exists to forbid, and would fail on
        `get_provider(name, **options)`, which it cannot read either way. Both
        verdicts are pinned here against calls written out for the purpose, so
        the scan's silence over `src/` is silence from a predicate that has
        been shown to speak.
        """
        control = (
            "get_provider(name)\n"
            "get_provider(name, registry=None)\n"
            "get_provider(name, registry=reg)\n"
            "get_provider(name, reg)\n"
            "get_provider(name, **options)\n"
            "get_provider(*args)\n"
            "get_provider(*args, registry=reg)\n"
            "load_custom_providers(declarations, True, registry=None)\n"
            "load_custom_providers(declarations, True, reg)\n"
        )
        position = self._watched_positions()
        calls = [
            node for node in ast.walk(ast.parse(control)) if isinstance(node, ast.Call)
        ]
        verdicts = [
            registry_argument(node, position[node.func.id])
            for node in sorted(calls, key=lambda node: node.lineno)
        ]
        self.assertEqual(
            verdicts,
            [
                "missing",
                "explicit-none",
                "supplied",
                "supplied",
                "unknown",
                "unknown",
                "supplied",
                "explicit-none",
                "supplied",
            ],
        )
        self.assertEqual(set(verdicts), set(CALL_SITE_VERDICTS))

    def test_every_production_call_site_passes_a_registry(self):
        """The static half, enumerated from the source tree rather than listed.

        `register_provider`, `get_provider` and `load_custom_providers` each
        fall back to the process global when the argument is omitted or is
        `None`, so the documented "fresh registry per operation" rests on every
        caller passing a real one. Which parameter carries it is read from each
        function's own signature, so a reordered signature cannot silently pass
        this, and the verdict is `registry_argument`'s, which the control above
        shows rejects a literal `None` as well as an omission.
        """
        position = self._watched_positions()
        package = Path(providers.__file__).resolve().parent
        found: List[Tuple[str, int, str]] = []
        unreadable: List[str] = []
        for path in sorted(package.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                called = (
                    func.id
                    if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute) else None
                )
                if called not in position:
                    continue
                verdict = registry_argument(node, position[called])
                site = f"{path.name}:{node.lineno}"
                found.append((path.name, node.lineno, called))
                if verdict == "unknown":
                    # Not a violation and not a pass: recorded, and asserted
                    # away below, so an unreadable call site cannot slip
                    # through as either.
                    unreadable.append(f"{site} {called}")
                    continue
                with self.subTest(site=site, call=called):
                    self.assertEqual(
                        verdict,
                        "supplied",
                        f"{site} calls {called} with registry {verdict}",
                    )
        self.assertEqual(
            unreadable, [], "a call site the scan cannot read; widen the predicate"
        )
        # The premise: an empty scan would pass without checking anything.
        self.assertGreaterEqual(len(found), len(position))
        self.assertEqual({call for _, _, call in found}, set(position))
        self.assertGreater(len({name for name, _, _ in found}), 1)


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-126: metadata validation and explain_diff containment
# ---------------------------------------------------------------------------

_DEEP = {"a": {"b": {"c": {"d": {"e": [1, 2, {"f": "g"}]}}}}}

#: Every shape the obligation names, at the explain_provider_diff boundary.
#: "Absent on one side" arrives as None, because the call site reads
#: `locked_comp.get("boundary_metadata")`.
METADATA_SHAPES: Tuple[Tuple[str, Any, Any], ...] = (
    ("a well-formed object", {"sig": "locked"}, {"sig": "current"}),
    ("absent on the old side", None, {"sig": "current"}),
    ("absent on the new side", {"sig": "locked"}, None),
    ("null on both sides", None, None),
    ("a list", [1, 2, 3], [4]),
    ("a string", "hostile", "other"),
    ("a number", 12345, 6.5),
    ("a boolean", True, False),
    ("a deeply nested object", _DEEP, _DEEP),
    ("a list against a string", ["x"], "y"),
)

#: The shapes a lockfile can actually carry past its own loader.
OBJECT_SHAPES = ("a well-formed object", "a deeply nested object")
NON_OBJECT_SHAPES = ("a list", "a string", "a number", "a boolean")


class _Absent:
    """Sentinel for "the key is not present", distinct from a null value."""

    def __repr__(self) -> str:  # pragma: no cover - failure messages only
        return "<absent>"


_ABSENT = _Absent()


class _DictAssumingProvider:
    """Written against the documented `Optional[dict]` signature, as a custom
    provider author would read it."""

    name = "custom.dict-assuming"
    version = "1"

    def resolve(self, ctx):  # pragma: no cover - never resolved here
        raise AssertionError("resolve is not part of this obligation")

    def explain_diff(self, old_metadata, new_metadata, ctx):
        return f"signature {old_metadata.get('sig')} -> {new_metadata.get('sig')}"


class _EchoingProvider:
    """The witness: it reports what it was handed, so a shape that never
    arrived cannot be mistaken for one that did."""

    name = "custom.echoing"
    version = "1"

    def resolve(self, ctx):  # pragma: no cover - never resolved here
        raise AssertionError("resolve is not part of this obligation")

    def explain_diff(self, old_metadata, new_metadata, ctx):
        return (
            f"echo old={type(old_metadata).__name__} new={type(new_metadata).__name__}"
        )


class _BaseExplodingProvider:
    name = "custom.base-exploding"
    version = "1"

    def resolve(self, ctx):  # pragma: no cover - never resolved here
        raise AssertionError("resolve is not part of this obligation")

    def explain_diff(self, old_metadata, new_metadata, ctx):
        raise BaseException("base exception escaping explain_diff")


def _explain_context() -> ProviderContext:
    return ProviderContext(
        repo_root=Path("."),
        component_path="svc",
        boundary_cfg={"provider": "path-hash", "paths": [SELECTOR]},
        source="head",
        read_file=lambda path: b"",
        list_files=lambda prefix: [],
    )


def rotated_digest(current: Optional[str]) -> str:
    """A well-formed SHA-256 digest that differs from *current* everywhere.

    The lockfile validator accepts a lowercase hex digest or null, so this is
    the cheapest way to give `why` a real boundary change to report without
    touching the tree - and every character differs, so the result cannot
    accidentally equal what it replaced. `leaf` records no digest at all, and
    None becomes a digest, which is a change too.
    """
    if current is None:
        return "0" * 64
    return "".join("1" if character == "0" else "0" for character in current)


class ProviderExplanationUnderHostileMetadataTests(unittest.TestCase):
    """OBL-GIT-SOURCE-126: one answer per provider, whatever the lockfile said."""

    def test_the_shapes_really_reach_the_provider(self):
        """The premise: without this, "unchanged" could mean "never delivered"."""
        answers = {
            label: explain_provider_diff(
                _EchoingProvider(), old, new, _explain_context()
            )
            for label, old, new in METADATA_SHAPES
        }
        self.assertEqual(answers["a list"], "echo old=list new=list")
        self.assertEqual(answers["a string"], "echo old=str new=str")
        self.assertEqual(answers["a number"], "echo old=int new=float")
        self.assertEqual(answers["a boolean"], "echo old=bool new=bool")
        self.assertEqual(answers["null on both sides"], "echo old=NoneType new=NoneType")
        self.assertGreaterEqual(len(set(answers.values())), 6)

    def test_every_registered_provider_gives_one_answer_for_every_shape(self):
        """The obligation, over the whole registry rather than over one provider.

        The expected string is not written down: it is whatever the provider
        answered for well-formed metadata, and every hostile shape has to
        reproduce it.
        """
        registry = create_registry()
        covered = set()
        for key in registry_keys():
            provider = registry[key]
            reference = explain_provider_diff(
                provider, {"sig": "locked"}, {"sig": "current"}, _explain_context()
            )
            covered.add(key)
            with self.subTest(provider=key, shape="reference"):
                # The premise for the invariance: a provider that returned the
                # empty string would make every comparison below trivial.
                self.assertTrue(reference.strip())
            for label, old, new in METADATA_SHAPES:
                with self.subTest(provider=key, shape=label):
                    self.assertEqual(
                        explain_provider_diff(provider, old, new, _explain_context()),
                        reference,
                    )
        self.assertEqual(covered, set(registry_keys()))

    def test_the_registered_providers_do_not_all_say_the_same_thing(self):
        """The premise for the loop above: the answers are provider-specific,
        so "every shape agreed" is a claim about each provider's own prose."""
        registry = create_registry()
        answers = {
            key: explain_provider_diff(
                registry[key], {"sig": "a"}, {"sig": "b"}, _explain_context()
            )
            for key in registry_keys()
        }
        self.assertGreaterEqual(len(set(answers.values())), 4)
        self.assertEqual(answers["json-canonical"], "JSON contract changed")
        self.assertEqual(answers["openapi-canonical"], "OpenAPI contract changed")
        self.assertEqual(answers["leaf"], "leaf component changed")

    def test_a_provider_that_assumes_a_dict_gets_the_decorated_fallback(self):
        """Not a traceback, and the fallback names the provider and the reason."""
        provider = _DictAssumingProvider()
        for label, old, new in METADATA_SHAPES:
            if label in OBJECT_SHAPES:
                continue
            with self.subTest(shape=label):
                answer = explain_provider_diff(provider, old, new, _explain_context())
                self.assertTrue(
                    answer.startswith(
                        f"{provider.name} boundary changed "
                        "(provider explanation unavailable: "
                    ),
                    answer,
                )
                self.assertIn("object has no attribute 'get'", answer)
        # And the premise: with a dict it answers normally, so the fallback
        # above is about the shape rather than about the provider.
        self.assertEqual(
            explain_provider_diff(
                provider, {"sig": "a"}, {"sig": "b"}, _explain_context()
            ),
            "signature a -> b",
        )

    def test_a_baseexception_is_folded_into_the_bounded_fallback(self):
        answer = explain_provider_diff(
            _BaseExplodingProvider(), {"a": 1}, {"a": 2}, _explain_context()
        )
        self.assertIn("boundary changed", answer)
        self.assertIn("provider explanation unavailable", answer)
        self.assertIn("base exception escaping explain_diff", answer)


class ProviderExplanationThroughTheCliTests(unittest.TestCase):
    """OBL-GIT-SOURCE-126, one layer up: what a corrupted lockfile actually does."""

    @classmethod
    def setUpClass(cls):
        cls.scene = Scenario("metadata126")
        for key in registry_keys():
            name = slug(key)
            cls.scene.component(
                name,
                path=f"c/{name}",
                provider=key,
                boundary=selectors_for(key),
            )
            cls.scene.file(f"c/{name}/{SELECTOR}", CONTRACT)
        cls.scene.commit()
        result = run_cli_in_process(cls.scene.root, "generate", "--source", "working-tree")
        assert result.returncode == 0, result.stderr
        cls.lock_path = cls.scene.root / "boundary.lock.json"
        cls.clean = json.loads(cls.lock_path.read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls.scene.close()

    def _why(
        self, component: str, value: Any, *, boundary_digest: Optional[str] = None
    ):
        """Run `why` against a lockfile carrying *value* as boundary_metadata.

        Passing *boundary_digest* also rewrites the locked boundary
        fingerprint, which is how a caller gives the report something to
        explain; leaving it None leaves the generated lock's own digest alone.
        """
        mutated = json.loads(json.dumps(self.clean))
        if value is _ABSENT:
            mutated["components"][component].pop("boundary_metadata", None)
        else:
            mutated["components"][component]["boundary_metadata"] = value
        if boundary_digest is not None:
            mutated["components"][component]["fingerprints"]["boundary"] = (
                boundary_digest
            )
        self.lock_path.write_text(
            json.dumps(mutated, indent=2) + "\n", encoding="utf-8"
        )
        result = run_cli_in_process(
            self.scene.root, "why", component, "--source", "working-tree"
        )
        detail = next(
            (
                line
                for line in result.stdout.splitlines()
                if line.startswith("Provider detail:")
            ),
            None,
        )
        return result, detail

    def test_no_builtin_records_boundary_metadata_of_its_own(self):
        """The premise: injecting a dict is a change, which is why it reports."""
        for key in registry_keys():
            with self.subTest(provider=key):
                self.assertNotIn(
                    "boundary_metadata", self.clean["components"][slug(key)]
                )

    def test_an_object_shape_reaches_the_provider_and_prints_its_prose(self):
        """The line's content, not merely a line beginning `Provider detail:`.

        Nine of the thirteen print the same sentence, so a regression in which
        `_output.py` stopped consulting the provider and printed one constant
        would satisfy "a detail line exists" and "the two object shapes agree"
        for every member. The expectation is therefore what
        `explain_provider_diff` itself answers for the injected metadata, which
        is the claim being made: the CLI prints what the provider said.
        """
        registry = create_registry()
        printed = {}
        for key in registry_keys():
            expected = "Provider detail: " + explain_provider_diff(
                registry[key], {"sig": "locked"}, None, _explain_context()
            )
            with self.subTest(provider=key):
                first, detail = self._why(slug(key), {"sig": "locked"})
                self.assertEqual(first.returncode, 1, first.stderr)
                self.assertEqual(detail, expected)
                nested, nested_detail = self._why(slug(key), _DEEP)
                self.assertEqual(nested.returncode, 1, nested.stderr)
                self.assertEqual(nested_detail, detail)
            printed[key] = detail
        # Two concrete facts about the prose, so that "the CLI printed what the
        # function returned" cannot be satisfied by both going constant at once.
        self.assertEqual(
            printed["path-hash"], "Provider detail: declared boundary artifact changed"
        )
        self.assertGreaterEqual(len(set(printed.values())), 5)

    def test_non_object_metadata_is_rejected_before_provider_dispatch(self):
        """Malformed lock data fails closed instead of reaching provider code."""
        shapes = {name: old for name, old, _ in METADATA_SHAPES}
        for key in registry_keys():
            for label in NON_OBJECT_SHAPES:
                with self.subTest(provider=key, shape=label):
                    result, detail = self._why(slug(key), shapes[label])
                    self.assertEqual(result.returncode, 2, result.stdout)
                    self.assertIsNone(detail)
                    self.assertIn(
                        f"LOCKFILE malformed: component '{slug(key)}' "
                        "boundary_metadata must be an object or null",
                        result.stderr,
                    )

    def test_null_and_absent_are_accepted_and_add_no_prose_of_their_own(self):
        """The other half of the pin, with the mechanism made to fire first.

        On an otherwise untouched lockfile both shapes exit 0, and the missing
        `Provider detail:` line records that `why` found no change at all: the
        gate in `_output.py` consults the provider only when the boundary
        fingerprint moved or the metadata did, and with a null on both sides
        neither has. Asserting the absence there is asserting nothing about
        null reaching `explain_diff`.

        So each member is run twice. The control rotates the locked boundary
        digest, which makes the explanation path run, and the same null and
        absent metadata then produce the provider's own sentence - the proof
        that these shapes are carried into `explain_diff` rather than
        intercepted. Only then does the quiet run mean what it says.

        The control asserts the printed line rather than the exit code, because
        gating is a question about the facet and not about the provider: `leaf`
        publishes no boundary digest, so its boundary drift is not gated and it
        exits 0 while still printing its prose.
        """
        registry = create_registry()
        for key in registry_keys():
            name = slug(key)
            locked = self.clean["components"][name]["fingerprints"]["boundary"]
            moved = rotated_digest(locked)
            self.assertNotEqual(moved, locked)
            expected = "Provider detail: " + explain_provider_diff(
                registry[key], None, None, _explain_context()
            )
            for shape, value in (("null", None), ("absent", _ABSENT)):
                with self.subTest(provider=key, shape=shape, run="control"):
                    result, detail = self._why(name, value, boundary_digest=moved)
                    self.assertNotEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(detail, expected)
                with self.subTest(provider=key, shape=shape, run="quiet"):
                    result, detail = self._why(name, value)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIsNone(detail)


# ---------------------------------------------------------------------------
# The closure the whole module rests on
# ---------------------------------------------------------------------------


class RegistryClosureTests(unittest.TestCase):
    """A provider added to the package must make this file red, not pass unseen."""

    def test_the_surface_is_read_from_the_public_runtime_authority(self):
        """A public export, and a fresh dict per call.

        The module global would have done at import time, but
        `load_custom_providers` and `register_provider` both write it
        when the argument is omitted, so a sibling test in this process
        could leave a name in it. `create_registry` is what production
        calls and it hands back new instances every time.
        """
        self.assertIn("create_registry", boundver.__all__)
        self.assertIs(boundver.create_registry, providers.create_registry)
        fresh, again = create_registry(), create_registry()
        self.assertIsNot(fresh, again)
        self.assertIsNot(fresh["path-hash"], again["path-hash"])
        self.assertIsNot(fresh["path-hash"], providers._REGISTRY["path-hash"])
        self.assertEqual(sorted(fresh), sorted(providers._REGISTRY))

    def test_the_registry_is_exactly_the_builtins_plus_the_aliases(self):
        """Derived from the two module constants that build it, not counted."""
        expected = {cls.name for cls in providers._BUILTIN_PROVIDER_TYPES}
        expected |= set(providers._ALIASES)
        self.assertEqual(set(registry_keys()), expected)
        self.assertTrue(registry_keys(), "the registry is empty")

    def test_every_registered_provider_is_classified_by_every_rule(self):
        """The one assertion that turns five tables into five checkers.

        Each obligation above places a provider using a rule rather than a
        list. This asserts the rules between them cover the registry exactly,
        so an unclassifiable new member cannot be silently skipped by all of
        them at once.
        """
        registry = create_registry()
        keys = set(registry)

        by_label: Dict[str, str] = {}
        by_mode: Dict[str, str] = {}
        for key in sorted(keys):
            entries = registry[key].resolve(_mode_context(key, "100644")).entries
            derived = label_class([label for label, _ in entries])
            self.assertIn(derived, INVALID_BYTES_OUTCOME, key)
            by_label[key] = derived
            if not entries:
                by_mode[key] = "no entries"
            else:
                by_mode[key] = "binding" if binds_git_mode(entries) else "dropping"
        self.assertEqual(set(by_label), keys)
        self.assertEqual(set(by_mode), keys)
        # Both rules are total - every entry list has a prefix set and a
        # content type - so the claim worth making is that neither has
        # collapsed: each class they can name still has a member.
        self.assertEqual(set(by_label.values()), set(INVALID_BYTES_OUTCOME))
        self.assertEqual(
            set(by_mode.values()), {"binding", "dropping", "no entries"}
        )

        by_v010 = {
            key
            for key in keys
            if key in _V010_RAW_BOUNDARY_PROVIDERS
            or key in _V010_CANONICAL_BOUNDARY_PROVIDERS
            or key in V010_RESIDUAL
        }
        self.assertEqual(by_v010, keys)

        by_explanation = {
            key
            for key in keys
            if explain_provider_diff(
                registry[key], {"a": 1}, {"a": 2}, _explain_context()
            ).strip()
        }
        self.assertEqual(by_explanation, keys)

    def test_the_thirteen_keys_are_not_thirteen_implementations(self):
        """What the enumeration buys, and what it does not.

        Every loop in this file runs once per registry key, and the subtest
        counts invite a reading the surface does not support: most of those
        runs exercise one function. `OpenApiProvider`, `JsonFileProvider`,
        `PythonExportsProvider` and `TypeScriptExportsProvider` add only a name
        to `PathHashProvider`, and the four `-raw` keys are the same instances
        again, so nine of the thirteen share a single `resolve` and ten agree
        on every digest here.

        That is not a defect in the loops - a member added tomorrow still has
        to be classified by a rule, and the alias keys still have to select the
        provider they name - but it is the honest size of the observation, and
        pinning the arithmetic keeps the module docstring answerable. If a
        provider grows its own `resolve`, this fails and the docstring gets
        rewritten rather than quietly becoming false.
        """
        registry = create_registry()
        keys = registry_keys()
        by_implementation: Dict[Any, List[str]] = {}
        for key in keys:
            by_implementation.setdefault(type(registry[key]).resolve, []).append(key)
        self.assertLess(
            len(by_implementation),
            len(keys),
            "every key has its own resolve; the docstring's caveat is stale",
        )
        largest = max(by_implementation.values(), key=len)
        self.assertGreater(
            len(largest),
            len(keys) // 2,
            f"no implementation backs a majority of the registry: {by_implementation}",
        )
        self.assertIn("path-hash", largest)


if __name__ == "__main__":
    unittest.main()
