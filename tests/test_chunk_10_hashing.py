"""Six promises about what a digest notices and what an exit code means.

Three of these obligations are about a filter that has to apply at exactly one
depth. `openapi-canonical` drops `info`, `servers` and `tags` before hashing,
because a team that bumps `info.version` on every release would otherwise
rotate the boundary digest on every release and learn to ignore the gate. The
same three names are legal deeper in the document, and there `servers` decides
where an operation is actually routed. So the interesting test is not that a
top-level edit is ignored, it is that the identical key one and two levels down
is not: a regression that changed the top-level dict comprehension in
`OpenApiCanonicalProvider._resolve` into a recursive strip would relocate a live
endpoint without moving the digest. The metadata half needs a premise of its
own, because "the digest did not move" is the same observation as "the edit
never reached the repository". Every stability row here therefore also asserts
that the `exact` facet did move, which proves the bytes changed and the drop is
what absorbed them.

`semantic_config_digest` is quantified over the whole configuration surface, so
the surface is read at runtime from the schema boundver itself ships rather than
listed by hand. The enumerator walks `boundary.config.schema.json` into 26 leaf
field paths, a fixture populates every one of them, and each is mutated in turn.
A field added to the schema tomorrow is mutated automatically and is required to
move the digest unless it is named in `PRESENTATION_FIELDS`; a field renamed out
of the schema makes the coverage test fail rather than quietly disappearing.
`PRESENTATION_FIELDS` mirrors everything `_semantic_config` deliberately
discards, including the schema URL, annotations, declaration-audit policy, and
slice descriptions. Ordering is the mirror image and is checked with the same machinery:
eight lists are sorted before hashing and reversing them must be invisible,
while `providers` is deliberately left in declaration order, so reversing it must
be visible. Each stability row is paired with an append to the same list, so a
digest that had stopped reading the list at all could not pass as one that
sorted it.

The exit-code obligations are about precedence, which needs both halves of an
issue list to exist at once. Exit 2 means boundver could not check reliably and
must outrank a gated compat mismatch, which is exit 5 and numerically higher.
The twelve safety prefixes are read out of `_drift_exit_code.__code__.co_consts`
so a new one is exercised automatically, and compared against the twelve the
reference documents so a deleted one fails. Constructing the collision for real
took a repository where one component drifts in `compat` while another cannot
supply a gated facet at all: scoping the run to the drifted component alone
gives 5, widening it to include the leaf gives 2, and the only difference is the
`UNAVAILABLE FACET` line. The baseline guards are enumerated the same way: the
three baseline modes come from the tuple in `_cmd_verify` and the nine bound
context fields come from calling `baseline_context`, because each is a list
somebody will extend.

The scope binding needs two halves and only one of them is a unit test. Feeding
a mutated context back to `apply_baseline` exercises one generic loop, so all
nine fields pass together and a field frozen to a constant would compare equal
to itself forever. The rows that carry the obligation therefore vary the
invocation and read the refusal by the name it prints: renaming the project
names `project`, relocking names `lock_digest`, giving one component its own
`verify_facets` names `policy_digest`, and reading the same clean tree as a
working tree names `source`. Renaming the project has to relock to get that far,
because a config whose project no longer matches the lock is rejected by
preflight before any baseline is read. That interception is pinned here too, and
it is the same reason `lock_schema` and `config_contract` cannot be varied end to
end: both are constants of the release, and a lock carrying anything else never
reaches `apply_baseline`.

What a capture must refuse is quantified over diagnostic classes, and the
witnesses are collected from live runs rather than remembered - an unparseable
OpenAPI document for `CURRENT DIGEST ERROR`, a lock whose component entry
records that failure for `LOCKED DIGEST ERROR`, a lock with its schema key
removed for `LOCKFILE`, a gated compat facet on a component with no version
source for `UNAVAILABLE FACET`. `violation_identity` is default-deny, so a table
of refusals proves nothing on its own; the same collected issues supply the real
`MISMATCH`, `SLICE MISMATCH` and `AFFECTED CONSUMERS` lines that are required to
produce identities, which is what makes "refused" an answer rather than the
answer.

Covers OBL-HASHING-033, OBL-HASHING-048, OBL-HASHING-049, OBL-HASHING-055,
OBL-HASHING-075 and OBL-HASHING-083.
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import boundver
from boundver import core
from boundver._baseline import (
    BaselineError,
    apply_baseline,
    baseline_context,
    create_baseline,
    violation_identity,
)
from boundver._lockfile import (
    LOCKFILE_SCHEMA,
    SEMANTIC_CONFIG_VERSION,
    semantic_config_digest,
    verify_lockfile,
)

from tests._parity import run_cli
from tests._scenarios import Scenario

# ---------------------------------------------------------------------------
# OBL-HASHING-033: the openapi-canonical top-level metadata drop
# ---------------------------------------------------------------------------

#: A document carrying every block the obligation names, at every depth that
#: matters: `servers` at the root, on the path item, and on the operation;
#: `tags` at the root and on the operation. The operation keeps an
#: `operationId` so there is a contract field to move as a control.
OPENAPI_DOCUMENT: Dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {"title": "orders", "version": "1.0.0"},
    "servers": [{"url": "https://api.example.invalid/v1"}],
    "tags": [{"name": "orders"}],
    "paths": {
        "/orders": {
            "servers": [{"url": "https://path-item.example.invalid"}],
            "get": {
                "operationId": "listOrders",
                "tags": ["orders"],
                "servers": [{"url": "https://operation.example.invalid"}],
                "responses": {"200": {"description": "ok"}},
            },
        }
    },
}

#: Sentinel for an edit that removes a key rather than replacing its value.
DELETE = object()

#: Edits to top-level metadata. Every one must leave the boundary digest
#: exactly where it was, because the provider drops these three blocks before
#: it canonicalizes anything.
METADATA_EDITS: Dict[str, Tuple[Tuple[Tuple[Any, ...], Any], ...]] = {
    "info.version bumped": ((("info", "version"), "2.0.0"),),
    "info.title renamed": ((("info", "title"), "orders-service"),),
    "servers[0].url repointed": (
        (("servers", 0, "url"), "https://replacement.example.invalid/v2"),
    ),
    "tags[0].name renamed": ((("tags", 0, "name"), "order-management"),),
    "all three blocks deleted": (
        (("info",), DELETE),
        (("servers",), DELETE),
        (("tags",), DELETE),
    ),
}

#: Edits below the root. The first three use a key that is in the top-level
#: drop set, one and two levels deeper than the filter is allowed to reach;
#: the last is the control that proves the provider hashes contract content.
CONTRACT_EDITS: Dict[str, Tuple[Tuple[Tuple[Any, ...], Any], ...]] = {
    "path-item servers repointed": (
        (
            ("paths", "/orders", "servers", 0, "url"),
            "https://relocated.example.invalid",
        ),
    ),
    "operation servers repointed": (
        (
            ("paths", "/orders", "get", "servers", 0, "url"),
            "https://relocated.example.invalid",
        ),
    ),
    "operation tags regrouped": (
        (("paths", "/orders", "get", "tags"), ["billing"]),
    ),
    "operationId renamed": (
        (("paths", "/orders", "get", "operationId"), "listAllOrders"),
    ),
}


def _edited(
    document: Dict[str, Any],
    changes: Sequence[Tuple[Tuple[Any, ...], Any]],
) -> Dict[str, Any]:
    """Return a copy of *document* with each (path, value) applied."""
    result = copy.deepcopy(document)
    for path, value in changes:
        node: Any = result
        for step in path[:-1]:
            node = node[step]
        if value is DELETE:
            del node[path[-1]]
        else:
            node[path[-1]] = value
    return result


class OpenApiMetadataDropDepthTests(unittest.TestCase):
    """OBL-HASHING-033: dropped at the root, hashed everywhere below it."""

    scene: Scenario
    base: Dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        cls.scene = Scenario()
        cls.scene.component(
            "svc", path="svc", provider="openapi-canonical", boundary=["api.json"]
        )
        cls.base = cls._publish(cls.scene, OPENAPI_DOCUMENT)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.scene.close()

    @staticmethod
    def _publish(scene: Scenario, document: Dict[str, Any]) -> Dict[str, Any]:
        """Commit *document* as the component's only boundary file."""
        scene.file("svc/api.json", json.dumps(document, indent=2) + "\n")
        scene.commit("publish")
        return scene.fingerprints("svc")

    def _facets_after(
        self, changes: Sequence[Tuple[Tuple[Any, ...], Any]]
    ) -> Dict[str, Any]:
        return self._publish(self.scene, _edited(OPENAPI_DOCUMENT, changes))

    def test_the_fixture_document_produces_a_boundary_digest_at_all(self):
        """The premise for everything else: the provider resolved."""
        self.assertIsNotNone(self.base["boundary"])
        self.assertNotEqual(self.base["boundary"], self.base["exact"])

    def test_a_top_level_metadata_edit_leaves_the_boundary_digest_alone(self):
        for label, changes in METADATA_EDITS.items():
            with self.subTest(edit=label):
                document = _edited(OPENAPI_DOCUMENT, changes)
                self.assertNotEqual(
                    document, OPENAPI_DOCUMENT, "the edit changed nothing"
                )
                after = self._facets_after(changes)
                self.assertEqual(
                    after["boundary"],
                    self.base["boundary"],
                    f"{label} rotated the boundary digest",
                )

    def test_the_same_edit_moves_the_exact_digest(self):
        """The premise: the edited bytes really did reach the repository.

        Without this, every row above is satisfied by a document that was
        never written, and the drop would be proven by a fixture failure.
        """
        for label, changes in METADATA_EDITS.items():
            with self.subTest(edit=label):
                after = self._facets_after(changes)
                self.assertNotEqual(
                    after["exact"],
                    self.base["exact"],
                    f"{label} never reached the committed tree",
                )

    def test_a_metadata_key_below_the_root_still_changes_the_digest(self):
        for label, changes in CONTRACT_EDITS.items():
            with self.subTest(edit=label):
                after = self._facets_after(changes)
                self.assertNotEqual(
                    after["boundary"],
                    self.base["boundary"],
                    f"{label} was absorbed by the top-level drop",
                )

    def test_removing_the_metadata_blocks_matches_editing_them(self):
        """The drop is total, not a normalization of the blocks' contents."""
        removed = self._facets_after(METADATA_EDITS["all three blocks deleted"])
        edited = self._facets_after(METADATA_EDITS["info.version bumped"])
        self.assertEqual(removed["boundary"], edited["boundary"])
        self.assertEqual(removed["boundary"], self.base["boundary"])


# ---------------------------------------------------------------------------
# OBL-HASHING-048 / OBL-HASHING-049: the semantic configuration surface
# ---------------------------------------------------------------------------

#: The schema boundver ships and validates configurations against. Reading it
#: from the installed package rather than the repository root keeps the
#: enumeration independent of the working directory.
CONFIG_SCHEMA_PATH = Path(boundver.__file__).resolve().parent / (
    "boundary.config.schema.json"
)

#: JSON Schema keywords whose presence means a node has structure below it.
#: A `oneOf` branch that carries none of these only constrains what is
#: required and would otherwise duplicate its parent as a leaf.
SCHEMA_STRUCTURE_KEYWORDS = (
    "properties",
    "items",
    "additionalProperties",
    "oneOf",
    "anyOf",
)

#: The presentation and declaration-audit fields `_semantic_config`
#: deliberately discards. Everything else
#: the schema declares is semantic by default, so a newly added field has to
#: move the digest or be argued about here.
PRESENTATION_FIELDS = frozenset(
    {
        ("$schema",),
        ("components", "*", "ecosystem"),
        ("components", "*", "note"),
        ("components", "*", "boundary", "note"),
        ("slices", "*", "description"),
        ("coverage", "source_indicators", "[]"),
        ("coverage", "exclusions", "[]", "paths", "[]"),
        ("coverage", "exclusions", "[]", "facets", "[]"),
        ("coverage", "exclusions", "[]", "reason"),
    }
)

#: The fields OBL-HASHING-048 names, spelled as schema paths. Keeping the
#: obligation's own list here means a schema rename fails this file rather
#: than silently narrowing what the checker covers.
OBLIGATION_FIELDS: Dict[str, Tuple[str, ...]] = {
    "project": ("project",),
    "providers": ("providers", "[]", "name"),
    "defaults.compat_mode": ("defaults", "compat_mode"),
    "defaults.verify_facets": ("defaults", "verify_facets", "[]"),
    "component path": ("components", "*", "path"),
    "boundary.provider": ("components", "*", "boundary", "provider"),
    "boundary.paths": ("components", "*", "boundary", "paths", "[]"),
    "boundary.options": ("components", "*", "boundary", "options"),
    "behavior.paths": ("components", "*", "behavior", "paths", "[]"),
    "version_source.file": ("components", "*", "version_source", "file"),
    "version_source.field": ("components", "*", "version_source", "field"),
    "version_source.git_tag_prefix": (
        "components", "*", "version_source", "git_tag_prefix",
    ),
    "version_source.component": (
        "components", "*", "version_source", "component",
    ),
    "version_source.constant": (
        "components", "*", "version_source", "constant",
    ),
    "derivation inputs": ("derivations", "*", "inputs", "[]"),
    "derivation outputs": ("derivations", "*", "outputs", "[]"),
    "derivation evidence": ("derivations", "*", "evidence"),
    "derivation generator": ("derivations", "*", "generator"),
    "vendored_copies": ("components", "*", "vendored_copies", "[]"),
    "consumers": ("components", "*", "consumers", "[]"),
    "external_consumers": ("components", "*", "external_consumers", "[]"),
    "component verify_facets": ("components", "*", "verify_facets", "[]"),
    "slice mode": ("slices", "*", "mode"),
    "slice components": ("slices", "*", "components", "[]"),
    "slice closure_of": ("slices", "*", "closure_of"),
}

#: Lists the docstring of `_semantic_config` calls sets: sorted before hashing,
#: so their declared order must be invisible. `providers` is the documented
#: exception and lives in its own table below.
SET_LIKE_LISTS: Dict[str, Tuple[str, ...]] = {
    "defaults.verify_facets": ("defaults", "verify_facets"),
    "boundary.paths": ("components", "*", "boundary", "paths"),
    "behavior.paths": ("components", "*", "behavior", "paths"),
    "vendored_copies": ("components", "*", "vendored_copies"),
    "consumers": ("components", "*", "consumers"),
    "external_consumers": ("components", "*", "external_consumers"),
    "component verify_facets": ("components", "*", "verify_facets"),
    "slice components": ("slices", "*", "components"),
    "derivation inputs": ("derivations", "*", "inputs"),
    "derivation outputs": ("derivations", "*", "outputs"),
}

#: The one list whose declaration order is retained on purpose, because a
#: custom provider's import-time behaviour depends on what was imported first.
ORDERED_LIST: Tuple[str, ...] = ("providers",)

#: One configuration populating every field the schema declares. Two
#: components and two slices are needed because `version_source` and the
#: slice membership keys are mutually exclusive one declaration at a time.
BASE_CONFIG: Dict[str, Any] = {
    "$schema": "https://example.invalid/boundary.config.schema.json",
    "project": "alpha",
    "providers": [
        {"module": "pkg.one", "class": "One", "name": "one"},
        {"module": "pkg.two", "class": "Two", "name": "two"},
    ],
    "defaults": {"compat_mode": "major", "verify_facets": ["boundary", "compat"]},
    "coverage": {
        "source_indicators": ["**/*.py", "**/*.ts"],
        "exclusions": [
            {
                "paths": ["generated/**", "vendor/**"],
                "facets": ["boundary", "ownership"],
                "reason": "fixture-only declaration coverage",
            }
        ],
    },
    "derivations": {
        "api": {
            "inputs": ["infra/template.yaml", "infra/routes.yaml"],
            "outputs": ["services/manifest/api/a.yaml", "services/manifest/api/b.yaml"],
            "evidence": "infra/api.boundver-derivation.json",
            "generator": "fixture/v1",
        }
    },
    "components": {
        "manifest": {
            "path": "services/manifest",
            "ecosystem": "python",
            "note": "a human annotation",
            "version_source": {"file": "pyproject.toml", "field": "project.version"},
            "boundary": {
                "provider": "openapi-canonical",
                "paths": ["api/a.yaml", "api/b.yaml"],
                "options": {"strict": True},
                "note": "a boundary annotation",
            },
            "behavior": {"paths": ["src/one.py", "src/two.py"]},
            "vendored_copies": ["vendor/a", "vendor/b"],
            "consumers": ["web", "cli"],
            "external_consumers": ["partner-a", "partner-b"],
            "verify_facets": ["boundary", "exact"],
        },
        "tagged": {
            "path": "services/tagged",
            "version_source": {"git_tag_prefix": "tagged-v"},
            "boundary": {"provider": "leaf", "paths": []},
        },
        "z-inherited": {
            "path": "services/inherited",
            "version_source": {"component": "manifest"},
            "boundary": {"provider": "leaf", "paths": []},
        },
        "z-constant": {
            "path": "services/constant",
            "version_source": {"constant": "1.2.3"},
            "boundary": {"provider": "leaf", "paths": []},
        },
    },
    "slices": {
        "members": {
            "description": "a slice description",
            "mode": "exact",
            "components": ["manifest", "tagged"],
        },
        "closure": {"mode": "boundary", "closure_of": "manifest"},
    },
}


def _schema_leaf_paths(node: Any, prefix: Tuple[str, ...] = ()) -> List[tuple]:
    """Every declared leaf field path, with `*` for a user-named map key."""
    if not isinstance(node, dict):
        return []
    found: List[tuple] = []
    properties = node.get("properties")
    if isinstance(properties, dict):
        for key, value in properties.items():
            found.extend(_schema_leaf_paths(value, prefix + (key,)))
    for branch in list(node.get("oneOf", ())) + list(node.get("anyOf", ())):
        if isinstance(branch, dict) and any(
            keyword in branch for keyword in SCHEMA_STRUCTURE_KEYWORDS
        ):
            found.extend(_schema_leaf_paths(branch, prefix))
    additional = node.get("additionalProperties")
    if isinstance(additional, dict):
        found.extend(_schema_leaf_paths(additional, prefix + ("*",)))
    items = node.get("items")
    if isinstance(items, dict):
        found.extend(_schema_leaf_paths(items, prefix + ("[]",)))
    return found or [prefix]


def _locate(node: Any, path: Sequence[str]) -> Optional[Tuple[Any, Any]]:
    """Return the (container, key) a schema path names in a config document.

    A `*` segment is resolved to the first user-named key under which the rest
    of the path exists, which is what lets one fixture cover two mutually
    exclusive spellings of the same declaration. `None` means the fixture does
    not populate the field, and that is a coverage failure, not a skip.
    """
    if not path:
        return None
    head, rest = path[0], path[1:]
    if head == "*":
        if not isinstance(node, dict):
            return None
        for key in sorted(node):
            if not rest:
                return (node, key)
            found = _locate(node[key], rest)
            if found is not None:
                return found
        return None
    if head == "[]":
        if not isinstance(node, list) or not node:
            return None
        return (node, 0) if not rest else _locate(node[0], rest)
    if not isinstance(node, dict) or head not in node:
        return None
    return (node, head) if not rest else _locate(node[head], rest)


def _mutated(config: Dict[str, Any], path: Sequence[str]) -> Dict[str, Any]:
    """Return a copy of *config* with one field changed, whatever its type."""
    candidate = copy.deepcopy(config)
    located = _locate(candidate, path)
    assert located is not None, path
    container, key = located
    current = container[key]
    if isinstance(current, bool):
        container[key] = not current
    elif isinstance(current, list):
        container[key] = current + ["zzz-appended"]
    elif isinstance(current, dict):
        container[key] = {"mutated": True}
    elif isinstance(current, str):
        container[key] = current + "-mutated"
    else:
        container[key] = "mutated"
    return candidate


def _reordered(config: Dict[str, Any], path: Sequence[str]) -> Dict[str, Any]:
    candidate = copy.deepcopy(config)
    located = _locate(candidate, path)
    assert located is not None, path
    container, key = located
    container[key] = list(reversed(container[key]))
    return candidate


SCHEMA = json.loads(CONFIG_SCHEMA_PATH.read_text(encoding="utf-8"))
SCHEMA_FIELDS = sorted(set(_schema_leaf_paths(SCHEMA)))


class SemanticConfigFieldCoverageTests(unittest.TestCase):
    """OBL-HASHING-048: no configuration field escapes the digest by accident."""

    def setUp(self) -> None:
        self.base = semantic_config_digest(BASE_CONFIG)

    def test_the_schema_enumeration_is_not_empty_or_degenerate(self):
        """The premise: the walk found real fields, not one root leaf."""
        self.assertGreater(len(SCHEMA_FIELDS), 20)
        self.assertIn(("project",), SCHEMA_FIELDS)
        self.assertIn(("components", "*", "boundary", "options"), SCHEMA_FIELDS)
        self.assertIn(("slices", "*", "closure_of"), SCHEMA_FIELDS)

    def test_the_fixture_populates_every_field_the_schema_declares(self):
        """A field added to the schema fails here until it is covered."""
        missing = [
            ".".join(path)
            for path in SCHEMA_FIELDS
            if _locate(BASE_CONFIG, path) is None
        ]
        self.assertEqual(missing, [], "not populated by BASE_CONFIG")

    def test_every_field_the_obligation_names_is_in_the_enumeration(self):
        for label, path in OBLIGATION_FIELDS.items():
            with self.subTest(field=label):
                self.assertIn(path, SCHEMA_FIELDS)

    def test_every_presentation_field_is_in_the_enumeration(self):
        """So a rename cannot quietly move a field out of the checker."""
        for path in sorted(PRESENTATION_FIELDS):
            with self.subTest(field=".".join(path)):
                self.assertIn(path, SCHEMA_FIELDS)

    def test_mutating_any_semantic_field_changes_the_digest(self):
        semantic = [path for path in SCHEMA_FIELDS if path not in PRESENTATION_FIELDS]
        self.assertGreaterEqual(len(semantic), 20)
        for path in semantic:
            with self.subTest(field=".".join(path)):
                candidate = _mutated(BASE_CONFIG, path)
                self.assertNotEqual(candidate, BASE_CONFIG)
                self.assertNotEqual(
                    semantic_config_digest(candidate),
                    self.base,
                    f"{'.'.join(path)} is excluded from semantic_config_digest",
                )

    def test_mutating_a_presentation_field_leaves_the_digest_alone(self):
        """The other half of the partition, with the same mutation helper.

        The test above is this one's premise: the identical machinery moves
        the digest for every other field in the same enumeration, so a stable
        digest here is the drop working rather than an edit that never landed.
        """
        for path in sorted(PRESENTATION_FIELDS):
            with self.subTest(field=".".join(path)):
                candidate = _mutated(BASE_CONFIG, path)
                self.assertNotEqual(candidate, BASE_CONFIG)
                self.assertEqual(semantic_config_digest(candidate), self.base)


class SemanticConfigListOrderTests(unittest.TestCase):
    """OBL-HASHING-049: sorted lists, and the one that is deliberately not."""

    def setUp(self) -> None:
        self.base = semantic_config_digest(BASE_CONFIG)

    def test_each_set_like_list_has_two_distinct_entries_to_reorder(self):
        """The premise: reversing these lists is a real change to the input."""
        for label, path in SET_LIKE_LISTS.items():
            with self.subTest(list=label):
                located = _locate(BASE_CONFIG, path)
                self.assertIsNotNone(located, label)
                container, key = located
                self.assertGreaterEqual(len(container[key]), 2)
                self.assertNotEqual(_reordered(BASE_CONFIG, path), BASE_CONFIG)

    def test_appending_to_each_set_like_list_changes_the_digest(self):
        """The premise: the digest reads these lists at all."""
        for label, path in SET_LIKE_LISTS.items():
            with self.subTest(list=label):
                self.assertNotEqual(
                    semantic_config_digest(_mutated(BASE_CONFIG, path)),
                    self.base,
                    f"{label} is not read by semantic_config_digest",
                )

    def test_reordering_a_set_like_list_leaves_the_digest_unchanged(self):
        for label, path in SET_LIKE_LISTS.items():
            with self.subTest(list=label):
                self.assertEqual(
                    semantic_config_digest(_reordered(BASE_CONFIG, path)),
                    self.base,
                    f"reordering {label} presented as contract drift",
                )

    def test_reordering_the_providers_array_changes_the_digest(self):
        reordered = _reordered(BASE_CONFIG, ORDERED_LIST)
        self.assertNotEqual(reordered, BASE_CONFIG)
        self.assertNotEqual(
            semantic_config_digest(reordered),
            self.base,
            "custom-provider declaration order stopped being semantic",
        )

    def test_the_providers_array_is_reordered_and_not_otherwise_edited(self):
        """The premise for the assertion above: only the order moved."""
        reordered = _reordered(BASE_CONFIG, ORDERED_LIST)
        self.assertEqual(
            sorted(reordered["providers"], key=lambda entry: entry["name"]),
            sorted(BASE_CONFIG["providers"], key=lambda entry: entry["name"]),
        )


# ---------------------------------------------------------------------------
# OBL-HASHING-055 / OBL-HASHING-083: exit 2 outranks facet severity
# ---------------------------------------------------------------------------

#: The twelve prefixes docs/reference.md and the obligation register describe
#: as "boundver could not check reliably". Held here as the independent
#: expectation for the tuple the implementation carries.
DOCUMENTED_SAFETY_PREFIXES = (
    "Config root",
    "LOCKFILE",
    "Custom provider loading failed",
    "Config invalid",
    "Config unavailable",
    "Verification error",
    "Unknown verification facet",
    "Unknown verification component",
    "Cannot capture",
    "Config malformed",
    "Lockfile schema mismatch",
    "CURRENT DIGEST ERROR",
    "LOCKED DIGEST ERROR",
    "DERIVATION ERROR",
    "UNAVAILABLE FACET",
    "DIAGNOSTICS TRUNCATED",
    "CONFIG SOURCE DIVERGENCE",
)

#: Read from the implementation, so a prefix added tomorrow is exercised by
#: the precedence table without anyone editing this file.
IMPLEMENTED_SAFETY_PREFIXES = next(
    const
    for const in core._drift_exit_code.__code__.co_consts
    if isinstance(const, tuple) and "DIAGNOSTICS TRUNCATED" in const
)

#: One gated mismatch per facet, at the severity `_drift_exit_code` assigns it.
FACET_MISMATCHES: Dict[str, Tuple[str, int]] = {
    "compat": ("MISMATCH svc.compat: lockfile=aaaa... current=bbbb...", 5),
    "boundary": ("MISMATCH svc.boundary: lockfile=aaaa... current=bbbb...", 4),
    "behavior": ("MISMATCH svc.behavior: lockfile=aaaa... current=bbbb...", 3),
    "slice compat": (
        "SLICE MISMATCH public.compat: lockfile=aaaa... current=bbbb...",
        5,
    ),
}


class SafetyPrefixPrecedenceTests(unittest.TestCase):
    """OBL-HASHING-083: a safety prefix wins against a higher facet severity."""

    def test_the_implementation_carries_exactly_the_documented_prefixes(self):
        self.assertEqual(
            list(IMPLEMENTED_SAFETY_PREFIXES), list(DOCUMENTED_SAFETY_PREFIXES)
        )

    def test_each_facet_mismatch_alone_produces_its_own_severity(self):
        """The premise: these issue strings really are classified as drift.

        Without this the precedence table below would pass against an issue
        list `_issue_facet` never matched, which returns 2 for reasons that
        have nothing to do with the safety prefixes.
        """
        for label, (issue, expected) in FACET_MISMATCHES.items():
            with self.subTest(mismatch=label):
                self.assertEqual(core._drift_exit_code([issue]), expected)
        self.assertEqual(
            core._drift_exit_code(["an issue nothing classifies"]), core.EXIT_DRIFT
        )

    def test_no_safety_prefix_alone_is_ever_anything_but_two(self):
        for prefix in IMPLEMENTED_SAFETY_PREFIXES:
            with self.subTest(prefix=prefix):
                self.assertEqual(
                    core._drift_exit_code([f"{prefix} could not be read"]),
                    core.EXIT_USAGE,
                )

    def test_every_safety_prefix_outranks_every_facet_mismatch(self):
        for prefix in IMPLEMENTED_SAFETY_PREFIXES:
            safety = f"{prefix} could not be read"
            for label, (mismatch, severity) in FACET_MISMATCHES.items():
                self.assertGreater(severity, core.EXIT_USAGE, label)
                for order, issues in (
                    ("safety first", [safety, mismatch]),
                    ("mismatch first", [mismatch, safety]),
                ):
                    with self.subTest(prefix=prefix, mismatch=label, order=order):
                        self.assertEqual(
                            core._drift_exit_code(issues),
                            core.EXIT_USAGE,
                            f"{prefix!r} was masked by a severity-{severity} facet",
                        )


def _compat_drift_repository() -> Scenario:
    """A repository where `svc` drifts in compat and `leafy` has no boundary.

    Gating a facet the leaf cannot supply is what produces a real
    `UNAVAILABLE FACET` issue, and the version bump on `svc` is what produces
    a real severity-5 compat mismatch. Both in one issue list is the
    collision OBL-HASHING-055 is about.
    """
    scene = Scenario()
    scene.component(
        "svc",
        path="svc",
        boundary=["api.json"],
        version_source={"file": "version.json", "field": "version"},
    )
    scene.component("leafy", path="leafy", provider="leaf")
    scene.file("svc/api.json", '{"a": 1}\n')
    scene.json_file("svc/version.json", {"version": "1.0.0"})
    scene.file("leafy/main.py", "x\n")
    scene.commit()
    generated = run_cli(scene.root, "generate", "--source", "head")
    assert generated.returncode == 0, generated.stderr
    scene.commit("lock")
    scene.json_file("svc/version.json", {"version": "2.0.0"})
    scene.commit("bump")
    return scene


class UnavailableFacetOutranksCompatDriftTests(unittest.TestCase):
    """OBL-HASHING-055: the same drift, exit 5 or exit 2 by what else is in scope."""

    scene: Scenario

    @classmethod
    def setUpClass(cls) -> None:
        cls.scene = _compat_drift_repository()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.scene.close()

    def _verify(self, *arguments: str):
        result = run_cli(
            self.scene.root, "verify", "--source", "head", "--format", "json",
            *arguments,
        )
        return result, json.loads(result.stdout)

    def test_the_compat_mismatch_alone_exits_five(self):
        """The premise: severity 5 is reachable in this repository."""
        result, payload = self._verify("--facets", "compat", "--components", "svc")
        self.assertEqual(result.returncode, core.EXIT_COMPAT)
        self.assertTrue(
            any(issue.startswith("MISMATCH svc.compat:") for issue in payload["issues"])
        )
        self.assertEqual(
            [
                issue
                for issue in payload["issues"]
                if issue.startswith(DOCUMENTED_SAFETY_PREFIXES)
            ],
            [],
        )

    def test_an_unavailable_facet_in_the_same_run_exits_two(self):
        result, payload = self._verify("--facets", "compat")
        self.assertTrue(
            any(issue.startswith("MISMATCH svc.compat:") for issue in payload["issues"]),
            payload["issues"],
        )
        self.assertTrue(
            any(
                issue.startswith("UNAVAILABLE FACET leafy.compat:")
                for issue in payload["issues"]
            ),
            payload["issues"],
        )
        self.assertEqual(result.returncode, core.EXIT_USAGE)

    def test_a_second_unavailable_facet_does_not_change_the_answer(self):
        result, payload = self._verify("--facets", "compat,boundary")
        self.assertGreaterEqual(
            len(
                [
                    issue
                    for issue in payload["issues"]
                    if issue.startswith("UNAVAILABLE FACET")
                ]
            ),
            2,
        )
        self.assertEqual(result.returncode, core.EXIT_USAGE)


class UpdateRefusesUnavailableFacetTests(unittest.TestCase):
    """OBL-HASHING-055: --update must not rewrite a lock it could not check."""

    def setUp(self) -> None:
        self.scene = _compat_drift_repository()
        self.addCleanup(self.scene.close)
        self.lock = self.scene.root / "boundary.lock.json"

    def test_update_rewrites_the_lock_when_only_the_compat_drift_remains(self):
        """The premise: --update does write in this repository."""
        before = self.lock.read_bytes()
        result = run_cli(
            self.scene.root, "verify", "--source", "head", "--facets", "compat",
            "--components", "svc", "--update", "--format", "json",
        )
        self.assertEqual(result.returncode, core.EXIT_OK)
        self.assertTrue(json.loads(result.stdout)["updated"])
        self.assertNotEqual(self.lock.read_bytes(), before)

    def test_update_leaves_the_lock_untouched_while_a_facet_is_unavailable(self):
        before = self.lock.read_bytes()
        result = run_cli(
            self.scene.root, "verify", "--source", "head", "--facets", "compat",
            "--update", "--format", "json",
        )
        payload = json.loads(result.stdout)
        self.assertEqual(result.returncode, core.EXIT_USAGE)
        self.assertFalse(payload["updated"])
        self.assertEqual(self.lock.read_bytes(), before)

    def test_the_refusal_says_the_lockfile_was_not_updated(self):
        result = run_cli(
            self.scene.root, "verify", "--source", "head", "--facets", "compat",
            "--update",
        )
        self.assertIn("LOCKFILE NOT UPDATED: ", result.stdout)
        self.assertIn(
            "`--update` will not modify the lock while these issues remain.",
            result.stdout,
        )


# ---------------------------------------------------------------------------
# OBL-HASHING-075: baseline write safety and scope binding
# ---------------------------------------------------------------------------

#: The three baseline modes, read from the guard in `_cmd_verify` so a fourth
#: is exercised by the exclusivity table the day it is added.
BASELINE_MODES = next(
    const
    for const in core._cmd_verify.__code__.co_consts
    if isinstance(const, tuple) and "write_baseline" in const
)

#: Flags a baseline mode refuses to be combined with, and the refusal each
#: must print. Both guards run before any file is read or written.
EXCLUSIVE_FLAGS: Dict[str, str] = {
    "--update": (
        "ERROR: verification baselines cannot be combined with lockfile --update"
    ),
    "--fail-fast": (
        "ERROR: verification baselines require the complete issue set; "
        "remove --fail-fast"
    ),
}

#: The diagnostic classes OBL-HASHING-075 says a ratchet may not acknowledge,
#: each spelled as the prefix its real emitter uses. The witnesses themselves
#: are collected from live verification runs rather than written here, so no row
#: can be satisfied by a string boundver never produces.
UNBASELINABLE_PREFIXES: Dict[str, str] = {
    "lockfile integrity": "LOCKFILE ",
    "configuration": "Config invalid: ",
    "ordinary metadata": "METADATA MISMATCH ",
    "current digest computation": "CURRENT DIGEST ERROR ",
    "locked digest computation": "LOCKED DIGEST ERROR ",
    "unavailable facet": "UNAVAILABLE FACET ",
}

#: One exact witness per class, recorded from the runs the collector performs.
#: The collected list has to contain these, so an emitter that is reworded
#: fails here instead of quietly leaving a class with no witness at all.
OBSERVED_WITNESSES: Dict[str, str] = {
    "lockfile integrity": f"LOCKFILE schema missing (expected {LOCKFILE_SCHEMA})",
    "configuration": (
        "Config invalid: Slice 'empty' field 'components' must contain at "
        "least one configured component; add a component name or remove the "
        "empty slice"
    ),
    "ordinary metadata": (
        "METADATA MISMATCH svc.version: lockfile='1.0.0' current='2.0.0'"
    ),
    "locked digest computation": (
        "LOCKED DIGEST ERROR svc: OpenAPI canonicalization failed for api.json"
    ),
    "unavailable facet": (
        "UNAVAILABLE FACET web.compat: selected gate requires both locked and "
        "current digests"
    ),
}

#: A string no emitter in boundver produces. `violation_identity` is
#: default-deny, so this is the one row where an invented witness is the point:
#: it is what every other row would look like if the emitters were renamed.
UNCLASSIFIED_ISSUE = "aaa0 a shape no boundver emitter has ever produced"

#: The refusal `apply_baseline` raises, named for the bound field that moved.
#: The CLI prints the same sentence behind an `ERROR: ` prefix.
BASELINE_REFUSAL = (
    "verification baseline {field} does not match this invocation; review the "
    "changed scope and explicitly update the baseline"
)

#: Where each baseline mode is pointed by the exclusivity table, and therefore
#: what "nothing happened" has to mean for it. `--write-baseline` needs a name
#: that does not exist yet; the other two need one that does.
BASELINE_MODE_DESTINATIONS: Dict[str, str] = {
    "baseline": "debt.json",
    "update_baseline": "debt.json",
    "write_baseline": "fresh.json",
}

#: The facet policy shape `_cmd_verify` hands to `baseline_context`.
EMPTY_FACET_POLICY = {
    "explicit": None,
    "defaults": None,
    "components": {},
    "slices": {},
}


def _drifted_repository() -> Scenario:
    """Two components, one drifted, and a lock committed before the drift."""
    scene = Scenario()
    scene.component("svc", path="svc", boundary=["api.json"])
    scene.component("other", path="other", boundary=["api.json"])
    scene.file("svc/api.json", '{"a": 1}\n')
    scene.file("other/api.json", '{"b": 1}\n')
    scene.commit()
    generated = run_cli(scene.root, "generate", "--source", "head")
    assert generated.returncode == 0, generated.stderr
    scene.commit("lock")
    scene.file("svc/api.json", '{"a": 2}\n')
    scene.commit("drift")
    return scene


def _context_of(scene: Scenario, **overrides: Any) -> Dict[str, Any]:
    """The context `_cmd_verify` would build for a default run of *scene*."""
    root = scene.root
    arguments: Dict[str, Any] = {
        "config": json.loads(
            (root / "boundary.config.json").read_text(encoding="utf-8")
        ),
        "lockfile": json.loads(
            (root / "boundary.lock.json").read_text(encoding="utf-8")
        ),
        "source": "head",
        "components_filter": [],
        "facets": None,
        "transitive": False,
        "facet_policy": EMPTY_FACET_POLICY,
    }
    arguments.update(overrides)
    return baseline_context(**arguments)


def _verify_issues(scene: Scenario, *arguments: str) -> List[str]:
    """The issue list one real verification run produced."""
    result = run_cli(
        scene.root, "verify", "--source", "head", "--format", "json", *arguments
    )
    return list(json.loads(result.stdout)["issues"])


def _baselined_repository() -> Scenario:
    """A drifted repository carrying a committed baseline for that drift.

    Both components drift, the baseline is captured while both do, and then
    `other` is repaired. That leaves all three modes with something to do:
    `--baseline` acknowledges what remains, `--update-baseline` really rewrites
    the file because the repaired violation drops out of it, and
    `--write-baseline` still has a fresh name to create. Without that, "the
    guard ran before the mode did" would be unobservable for two of the three,
    since neither reading mode ever creates its destination.
    """
    scene = Scenario()
    scene.component("svc", path="svc", boundary=["api.json"])
    scene.component("other", path="other", boundary=["api.json"])
    scene.file("svc/api.json", '{"a": 1}\n')
    scene.file("other/api.json", '{"b": 1}\n')
    scene.commit()
    generated = run_cli(scene.root, "generate", "--source", "head")
    assert generated.returncode == 0, generated.stderr
    scene.commit("lock")
    scene.file("svc/api.json", '{"a": 2}\n')
    scene.file("other/api.json", '{"b": 2}\n')
    scene.commit("drift both components")
    captured = run_cli(
        scene.root, "verify", "--source", "head", "--write-baseline", "debt.json"
    )
    assert captured.returncode == 0, captured.stderr
    scene.git("add", "--all")
    scene.git("commit", "-m", "baseline")
    scene.file("other/api.json", '{"b": 1}\n')
    scene.commit("repair one of them")
    return scene


class BaselineFlagExclusivityTests(unittest.TestCase):
    """OBL-HASHING-075: no baseline mode may run against a partial issue set."""

    scene: Scenario
    captured: bytes

    @classmethod
    def setUpClass(cls) -> None:
        cls.scene = _baselined_repository()
        cls.captured = (cls.scene.root / "debt.json").read_bytes()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.scene.close()

    def setUp(self) -> None:
        self.debt = self.scene.root / "debt.json"
        self.fresh = self.scene.root / "fresh.json"
        self._reset_destinations()

    def _verify(self, *arguments: str):
        return run_cli(self.scene.root, "verify", "--source", "head", *arguments)

    def _reset_destinations(self) -> None:
        """Undo whatever the previous row did, so no row inherits a leak.

        The rows share one repository, and a row whose guard has stopped firing
        writes to these paths. Without this the next row would fail on the
        wreckage rather than on its own subject. Only the committed baseline
        and the capture destination are ever written, so restoring those two is
        the whole reset.
        """
        self.debt.write_bytes(self.captured)
        if self.fresh.exists():
            self.fresh.unlink()

    def test_the_guard_enumerates_the_three_documented_modes(self):
        self.assertEqual(
            list(BASELINE_MODES), ["baseline", "write_baseline", "update_baseline"]
        )
        self.assertEqual(
            sorted(BASELINE_MODE_DESTINATIONS), sorted(BASELINE_MODES),
            "a mode was added to _cmd_verify without a destination to test it at",
        )

    def test_fail_fast_on_its_own_is_accepted(self):
        """The premise: the flag is not rejected for some unrelated reason."""
        result = self._verify("--fail-fast")
        self.assertEqual(result.returncode, core.EXIT_BOUNDARY)
        self.assertEqual(result.stderr.strip(), "")

    def test_each_mode_alone_does_something_to_its_destination(self):
        """The premise for every "nothing happened" claim in the table below.

        Each mode leaves a different trace, so each row's absence assertion has
        to be a different one: applying prints an acknowledgement and exits 0,
        updating rewrites the file, capturing creates it.
        """
        self._reset_destinations()
        applied = self._verify("--baseline", "debt.json")
        self.assertEqual(applied.returncode, core.EXIT_OK, applied.stderr)
        self.assertIn("Acknowledged 2 known baseline violation(s)", applied.stdout)

        self._reset_destinations()
        updated = self._verify("--update-baseline", "debt.json")
        self.assertEqual(updated.returncode, core.EXIT_OK, updated.stderr)
        self.assertIn("Baseline updated at debt.json", updated.stdout)
        self.assertNotEqual(
            self.debt.read_bytes(),
            self.captured,
            "the repaired violation was expected to drop out of the file",
        )

        self._reset_destinations()
        created = self._verify("--write-baseline", "fresh.json")
        self.assertEqual(created.returncode, core.EXIT_OK, created.stderr)
        self.assertTrue(self.fresh.is_file())

    def test_no_baseline_mode_can_be_combined_with_update_or_fail_fast(self):
        for mode, destination in BASELINE_MODE_DESTINATIONS.items():
            flag = "--" + mode.replace("_", "-")
            for conflicting, message in EXCLUSIVE_FLAGS.items():
                with self.subTest(mode=flag, conflicts_with=conflicting):
                    self._reset_destinations()
                    result = self._verify(flag, destination, conflicting)
                    self.assertEqual(result.returncode, core.EXIT_USAGE)
                    self.assertEqual(result.stderr.strip(), message)
                    self.assertEqual(
                        result.stdout, "", "the run got as far as reporting"
                    )
                    self.assertEqual(
                        self.debt.read_bytes(),
                        self.captured,
                        "the guard ran after the update",
                    )
                    self.assertFalse(
                        self.fresh.exists(), "the guard ran after the capture"
                    )


class BaselineDestinationTests(unittest.TestCase):
    """OBL-HASHING-075: a capture may not land on the config or the lock."""

    scene: Scenario

    @classmethod
    def setUpClass(cls) -> None:
        cls.scene = _drifted_repository()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.scene.close()

    def setUp(self) -> None:
        # The only thing any row here writes is the ordinary destination, and
        # one row needs it absent, so removing it is the whole reset.
        destination = self.scene.root / "debt.json"
        if destination.exists():
            destination.unlink()

    def test_an_ordinary_destination_is_written(self):
        """The premise: this repository can capture a baseline at all."""
        result = run_cli(
            self.scene.root, "verify", "--source", "head",
            "--write-baseline", "debt.json",
        )
        self.assertEqual(result.returncode, core.EXIT_OK, result.stderr)
        document = json.loads(
            (self.scene.root / "debt.json").read_text(encoding="utf-8")
        )
        self.assertTrue(document["violations"])

    def test_an_existing_destination_is_refused_for_being_existing(self):
        """The premise that gives the test below its teeth.

        Both the config and the lockfile already exist, so a capture aimed at
        either would be refused by the create-only rule even with no destination
        guard at all. This is the message that rule prints, and it is not the
        message the destination guard prints, which is why asserting the exact
        line below distinguishes the two.
        """
        first = run_cli(
            self.scene.root, "verify", "--source", "head",
            "--write-baseline", "debt.json",
        )
        self.assertEqual(first.returncode, core.EXIT_OK, first.stderr)
        again = run_cli(
            self.scene.root, "verify", "--source", "head",
            "--write-baseline", "debt.json",
        )
        self.assertEqual(again.returncode, core.EXIT_USAGE)
        self.assertEqual(
            again.stderr.strip(),
            "ERROR: verification baseline already exists: debt.json; use "
            "--update-baseline after reviewing current debt",
        )

    def test_the_config_and_the_lockfile_are_refused_as_destinations(self):
        for target in ("boundary.config.json", "boundary.lock.json"):
            with self.subTest(destination=target):
                before = (self.scene.root / target).read_bytes()
                result = run_cli(
                    self.scene.root, "verify", "--source", "head",
                    "--write-baseline", target,
                )
                self.assertEqual(result.returncode, core.EXIT_USAGE)
                self.assertEqual(
                    result.stderr.strip(),
                    "ERROR: verification baseline must not overwrite the config "
                    "or lockfile",
                    "the destination guard did not fire; the create-only rule did",
                )
                self.assertEqual(
                    (self.scene.root / target).read_bytes(),
                    before,
                    "the refused run still touched the file",
                )


class BaselineContextBindingTests(unittest.TestCase):
    """OBL-HASHING-075: `apply_baseline` checks every field it is handed.

    That is all this class can establish, and saying so matters. The comparison
    is one generic loop over `context.items()`, so the nine rows below exercise
    one line nine times; they prove no field is exempt from it and that the
    refusal names the field that moved, and they would stay green if
    `baseline_context` stopped deriving a field from the invocation and returned
    a constant instead. `BaselineInvocationDerivationTests` is the half that
    varies the invocation.
    """

    scene: Scenario
    context: Dict[str, Any]
    document: Dict[str, Any]
    ISSUES: List[str]

    @classmethod
    def setUpClass(cls) -> None:
        cls.scene = _drifted_repository()
        # Real issues from a real run, so the acknowledgement below is not
        # measured against a shape boundver does not emit.
        cls.ISSUES = _verify_issues(cls.scene)
        cls.context = _context_of(cls.scene)
        cls.document = create_baseline(cls.context, cls.ISSUES)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.scene.close()

    @staticmethod
    def _altered(context: Dict[str, Any], field: str) -> Dict[str, Any]:
        candidate = dict(context)
        value = candidate[field]
        if isinstance(value, bool):
            candidate[field] = not value
        elif isinstance(value, list):
            candidate[field] = value + ["other"]
        elif value is None:
            candidate[field] = ["boundary"]
        else:
            candidate[field] = f"{value}-other"
        return candidate

    def test_the_context_binds_the_nine_documented_fields(self):
        self.assertEqual(
            sorted(self.context),
            [
                "components_filter",
                "config_contract",
                "facets",
                "lock_digest",
                "lock_schema",
                "policy_digest",
                "project",
                "source",
                "transitive",
            ],
        )

    def test_the_drift_this_class_baselines_is_real_and_baselinable(self):
        """The premise: the fixture issues came out of a verification run."""
        self.assertNotEqual(self.ISSUES, [])
        for issue in self.ISSUES:
            self.assertRegex(issue, r"^MISMATCH svc\.(boundary|exact): lockfile=")

    def test_the_capture_carries_every_bound_field_into_the_document(self):
        """The premise for the refusals: the document records what to compare."""
        for field, value in self.context.items():
            with self.subTest(field=field):
                self.assertIn(field, self.document)
                self.assertEqual(self.document[field], value)

    def test_an_unaltered_context_applies_and_acknowledges_the_issue(self):
        """The premise: the refusals below are the binding, not a broken fixture."""
        new, acknowledged, stale = apply_baseline(
            self.document, self.context, self.ISSUES
        )
        self.assertEqual(new, [])
        self.assertEqual(acknowledged, self.ISSUES)
        self.assertEqual(stale, [])

    def test_altering_any_bound_field_refuses_the_baseline_by_name(self):
        """No field is exempt from the comparison, and each is named on refusal.

        This is the weak half of the binding by construction: it alters the
        dictionary `baseline_context` returned rather than the invocation it was
        derived from. What it rules out is a field silently dropped from the
        comparison loop, which is a real regression and not the one the
        derivation tests cover.
        """
        for field in sorted(self.context):
            with self.subTest(field=field):
                altered = self._altered(self.context, field)
                self.assertNotEqual(altered[field], self.context[field])
                with self.assertRaises(BaselineError) as caught:
                    apply_baseline(self.document, altered, self.ISSUES)
                self.assertEqual(
                    str(caught.exception), BASELINE_REFUSAL.format(field=field)
                )


class BaselineScopeBindingEndToEndTests(unittest.TestCase):
    """OBL-HASHING-075: a narrower capture cannot forgive a wider run.

    Three of the nine bound fields are set by flags alone, so these rows vary
    the invocation without editing a file: `--components`, `--facets` and
    `--transitive`. The other six need the repository itself to change, and
    those live in `BaselineInvocationDerivationTests`.
    """

    scene: Scenario

    @classmethod
    def setUpClass(cls) -> None:
        cls.scene = _drifted_repository()
        captured = run_cli(
            cls.scene.root, "verify", "--source", "head",
            "--components", "svc", "--write-baseline", "debt.json",
        )
        assert captured.returncode == 0, captured.stderr
        cls.scene.git("add", "--all")
        cls.scene.git("commit", "-m", "baseline")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.scene.close()

    def _verify(self, *arguments: str):
        return run_cli(
            self.scene.root, "verify", "--source", "head",
            "--baseline", "debt.json", *arguments,
        )

    def test_the_capture_recorded_the_narrow_scope(self):
        document = json.loads(
            (self.scene.root / "debt.json").read_text(encoding="utf-8")
        )
        self.assertEqual(document["components_filter"], ["svc"])
        self.assertIsNone(document["facets"])
        self.assertFalse(document["transitive"])

    def test_applying_it_under_the_same_scope_succeeds(self):
        """The premise: this baseline does silence this drift."""
        result = self._verify("--components", "svc")
        self.assertEqual(result.returncode, core.EXIT_OK, result.stderr)

    def test_applying_it_under_a_different_scope_is_refused(self):
        cases = {
            "no component filter": ((), "components_filter"),
            "explicit facets": (("--components", "svc", "--facets", "boundary"),
                                "facets"),
            "transitive consumers": (("--components", "svc", "--transitive"),
                                     "transitive"),
        }
        for label, (arguments, field) in cases.items():
            with self.subTest(scope=label):
                result = self._verify(*arguments)
                self.assertEqual(result.returncode, core.EXIT_USAGE)
                self.assertEqual(
                    result.stderr.strip(),
                    "ERROR: " + BASELINE_REFUSAL.format(field=field),
                )


class BaselineInvocationDerivationTests(unittest.TestCase):
    """OBL-HASHING-075: each bound field is derived from this invocation.

    The unit-level table proves `apply_baseline` compares every field it is
    handed. It cannot prove `baseline_context` built those fields from the run,
    and that is the half the obligation's gap is about: a field frozen to a
    constant compares equal to itself, the whole table stays green, and a
    baseline captured under one scope silently forgives a run under another.
    These rows change the repository or the flags instead and read the field
    name out of the refusal, which is the only externally visible evidence of
    which comparison failed.

    One repository serves them all, reset to the commit that carries the
    capture after every row, because building and baselining a repository per
    row costs several seconds each and the rows are independent.
    """

    scene: Scenario
    head: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.scene = _drifted_repository()
        captured = run_cli(
            cls.scene.root, "verify", "--source", "head",
            "--write-baseline", "debt.json",
        )
        assert captured.returncode == 0, captured.stderr
        cls.scene.git("add", "--all")
        cls.scene.git("commit", "-m", "baseline")
        cls.head = cls.scene.head()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.scene.close()

    def setUp(self) -> None:
        self.declaration = copy.deepcopy(self.scene.config)
        self.lock = self.scene.root / "boundary.lock.json"
        self.locked_bytes = self.lock.read_bytes()
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        """Put the repository back on the commit that carries the capture."""
        self.scene.config = self.declaration
        self.scene.git("reset", "--hard", self.head)
        self.scene.git("clean", "-fd")

    def _verify(self, *arguments: str):
        return run_cli(
            self.scene.root, "verify", "--source", "head",
            "--baseline", "debt.json", *arguments,
        )

    def _relock(self) -> None:
        regenerated = run_cli(self.scene.root, "generate", "--source", "head")
        self.assertEqual(regenerated.returncode, 0, regenerated.stderr)
        self.scene.commit("relock")
        self.assertNotEqual(
            self.lock.read_bytes(),
            self.locked_bytes,
            "the relock did not move the lockfile, so lock_digest cannot move",
        )

    def test_the_stored_capture_applies_to_an_unchanged_invocation(self):
        """The premise: every refusal below is the change, not the fixture."""
        result = self._verify()
        self.assertEqual(result.returncode, core.EXIT_OK, result.stderr)

    def test_relocking_alone_is_refused_by_lock_digest(self):
        """`lock_digest` is derived: a new lock under the same config refuses.

        This is also the control for the row below. Both rows move the lock;
        only one of them also renames the project, and they are told apart by
        which field the refusal names.
        """
        self._relock()
        result = self._verify()
        self.assertEqual(result.returncode, core.EXIT_USAGE, result.stderr)
        self.assertEqual(
            result.stderr.strip(),
            "ERROR: " + BASELINE_REFUSAL.format(field="lock_digest"),
        )

    def test_a_renamed_project_is_refused_by_project_and_not_by_lock_digest(self):
        """`project` is derived from the config this run loaded.

        The rename forces a relock, so two bound fields move at once and the
        refusal has to choose. It names `project`, which comes first in the
        binding; a `project` frozen to a constant would leave the run
        indistinguishable from the control above and the refusal would name
        `lock_digest` instead.
        """
        self.scene.config["project"] = "renamed"
        self.scene.commit("rename the project")
        self._relock()
        result = self._verify()
        self.assertEqual(result.returncode, core.EXIT_USAGE, result.stderr)
        self.assertEqual(
            result.stderr.strip(),
            "ERROR: " + BASELINE_REFUSAL.format(field="project"),
        )

    def test_a_project_change_without_a_relock_never_reaches_the_baseline(self):
        """Why the row above has to relock, pinned rather than assumed."""
        self.scene.config["project"] = "renamed"
        self.scene.commit("rename the project")
        result = self._verify()
        self.assertEqual(result.returncode, core.EXIT_DRIFT, result.stderr)
        self.assertIn("ERROR: lockfile preflight failed:", result.stderr)
        self.assertIn(
            "  - METADATA MISMATCH project: lockfile='scenario' "
            "current='renamed'",
            result.stderr,
        )
        self.assertNotIn("verification baseline", result.stderr)

    def test_giving_one_component_its_own_facets_is_refused_by_policy_digest(self):
        """`policy_digest` is derived from the effective facet policy.

        Nothing on the command line changes here: `--facets` stays unset, so
        the `facets` field is `None` on both sides and only the policy the
        config implies has moved. This is the amnesty the field exists to
        prevent - a baseline captured under a narrow per-component gate being
        applied to a run under a wider one.
        """
        stored = json.loads(
            (self.scene.root / "debt.json").read_text(encoding="utf-8")
        )
        self.assertIsNone(stored["facets"], "this row must not move `facets`")
        self.scene.config["components"]["other"]["verify_facets"] = ["boundary"]
        self.scene.commit("gate one component")
        result = self._verify()
        self.assertEqual(result.returncode, core.EXIT_USAGE, result.stderr)
        self.assertEqual(
            result.stderr.strip(),
            "ERROR: " + BASELINE_REFUSAL.format(field="policy_digest"),
        )

    def test_reading_the_same_clean_tree_as_a_working_tree_is_refused(self):
        """`source` is derived from the flag, not from what the run observed."""
        from_head = run_cli(
            self.scene.root, "verify", "--source", "head", "--format", "json"
        )
        from_tree = run_cli(
            self.scene.root, "verify", "--source", "working-tree",
            "--format", "json",
        )
        self.assertEqual(
            json.loads(from_head.stdout)["issues"],
            json.loads(from_tree.stdout)["issues"],
            "the two source modes disagree here, so this row would prove nothing",
        )
        result = run_cli(
            self.scene.root, "verify", "--source", "working-tree",
            "--baseline", "debt.json",
        )
        self.assertEqual(result.returncode, core.EXIT_USAGE, result.stderr)
        self.assertEqual(
            result.stderr.strip(),
            "ERROR: " + BASELINE_REFUSAL.format(field="source"),
        )

    def test_the_two_contract_fields_are_constants_of_this_release(self):
        """Known ceiling: `lock_schema` and `config_contract` cannot be varied.

        Both are release constants rather than properties of an invocation, and
        a lock carrying anything else is rejected by preflight before a baseline
        is read - which this row demonstrates rather than asserts from memory.
        The unit-level table is the honest limit for those two fields.
        """
        stored = json.loads(
            (self.scene.root / "debt.json").read_text(encoding="utf-8")
        )
        self.assertEqual(stored["lock_schema"], LOCKFILE_SCHEMA)
        self.assertEqual(stored["config_contract"], SEMANTIC_CONFIG_VERSION)
        lockfile = json.loads(self.lock.read_text(encoding="utf-8"))
        lockfile["config_contract"] = "boundver-semantic-config/v99"
        self.lock.write_text(
            json.dumps(lockfile, indent=2) + "\n", encoding="utf-8"
        )
        self.scene.commit("an unsupported contract")
        result = self._verify()
        self.assertEqual(result.returncode, core.EXIT_USAGE, result.stderr)
        self.assertIn("ERROR: lockfile preflight failed:", result.stderr)
        self.assertIn(
            "  - LOCKFILE semantic configuration contract mismatch: "
            f"'{LOCKFILE_SCHEMA}' uses 'boundver-semantic-config/v99', but "
            "boundver ",
            result.stderr,
        )
        self.assertNotIn("verification baseline", result.stderr)


def _consumer_drift_repository() -> Scenario:
    """One drifted component with a version source, a consumer and a slice.

    This supplies most of the collected witnesses in one repository: the
    version bump gives a compat mismatch with its ancillary version and semver
    metadata, the consumer gives an `AFFECTED CONSUMERS` line, the slice gives
    a `SLICE MISMATCH`, and gating compat over `web`, which has no version
    source, gives an `UNAVAILABLE FACET`.
    """
    scene = Scenario()
    scene.component(
        "svc",
        path="svc",
        boundary=["api.json"],
        version_source={"file": "version.json", "field": "version"},
        consumers=["web"],
    )
    scene.component("web", path="web", boundary=["api.json"])
    scene.slice("public", mode="exact", components=["svc", "web"])
    scene.file("svc/api.json", '{"a": 1}\n')
    scene.json_file("svc/version.json", {"version": "1.0.0"})
    scene.file("web/api.json", '{"b": 1}\n')
    scene.commit()
    generated = run_cli(scene.root, "generate", "--source", "head")
    assert generated.returncode == 0, generated.stderr
    scene.commit("lock")
    scene.file("svc/api.json", '{"a": 2}\n')
    scene.json_file("svc/version.json", {"version": "2.0.0"})
    scene.commit("drift")
    return scene


def _digest_error_issues() -> List[str]:
    """Real `CURRENT`/`LOCKED DIGEST ERROR` and `LOCKFILE` lines.

    `generate` refuses to write a lock whose provider failed, with or without
    `--allow-partial`, so a locked entry recording such a failure can only
    arrive by hand or from another release. The message inside that entry is
    this fixture's; the shape around it, `LOCKED DIGEST ERROR <component>: `,
    is boundver's, and that is the part the refusal table is about.
    """
    with Scenario() as scene:
        scene.component(
            "svc", path="svc", provider="openapi-canonical", boundary=["api.json"]
        )
        scene.json_file("svc/api.json", OPENAPI_DOCUMENT)
        scene.commit()
        generated = run_cli(scene.root, "generate", "--source", "head")
        assert generated.returncode == 0, generated.stderr
        scene.commit("lock")
        lock_path = scene.root / "boundary.lock.json"
        scene.file("svc/api.json", "{not a document\n")
        scene.commit("break the document")
        collected = _verify_issues(scene)
        scene.json_file("svc/api.json", OPENAPI_DOCUMENT)
        lockfile = json.loads(lock_path.read_text(encoding="utf-8"))
        entry = lockfile["components"]["svc"]
        entry["boundary_status"] = "error"
        entry["boundary_errors"] = [
            "OpenAPI canonicalization failed for api.json"
        ]
        lock_path.write_text(
            json.dumps(lockfile, indent=2) + "\n", encoding="utf-8"
        )
        scene.commit("a lock that records the failure")
        collected += _verify_issues(scene)
        del lockfile["schema"]
        lock_path.write_text(
            json.dumps(lockfile, indent=2) + "\n", encoding="utf-8"
        )
        scene.commit("a lock with no schema")
        collected += _verify_issues(scene)
    return collected


def _configuration_issues(scene: Scenario) -> List[str]:
    """The `Config invalid:` line, which only the library API can reach.

    The CLI validates the configuration first, so an empty explicit slice is
    reported there as a schema error and verification never runs. Embedders
    call `verify_lockfile` directly, and this diagnostic is what guards that
    entry point - so the witness is collected from the same function the CLI
    calls rather than invented.
    """
    config = copy.deepcopy(scene.config)
    config.setdefault("slices", {})["empty"] = {"mode": "exact", "components": []}
    lockfile = json.loads(
        (scene.root / "boundary.lock.json").read_text(encoding="utf-8")
    )
    return list(verify_lockfile(config, lockfile, scene.root, source="head"))


class UnbaselinableDiagnosticTests(unittest.TestCase):
    """OBL-HASHING-075: a ratchet may only acknowledge repairable drift.

    `violation_identity` is default-deny: three regexes say yes and everything
    else returns `None`. A table of refusals is therefore trivially satisfiable
    by any string at all, including one no emitter produces, which is why every
    witness here is collected from a live run and why the positive side of the
    partition is asserted alongside it.
    """

    scene: Scenario
    context: Dict[str, Any]
    observed: List[str]
    witnesses: Dict[str, List[str]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.scene = _consumer_drift_repository()
        cls.context = _context_of(cls.scene)
        cls.observed = _verify_issues(cls.scene) + _verify_issues(
            cls.scene, "--facets", "compat"
        )
        collected: Dict[str, List[str]] = {
            label: [] for label in UNBASELINABLE_PREFIXES
        }
        everything = (
            cls.observed + _configuration_issues(cls.scene) + _digest_error_issues()
        )
        for issue in everything:
            for label, prefix in UNBASELINABLE_PREFIXES.items():
                if issue.startswith(prefix):
                    collected[label].append(issue)
        cls.witnesses = {
            label: sorted(set(issues)) for label, issues in collected.items()
        }

    @classmethod
    def tearDownClass(cls) -> None:
        cls.scene.close()

    def _selected(self, prefix: str) -> List[str]:
        return [issue for issue in self.observed if issue.startswith(prefix)]

    def test_a_real_component_facet_mismatch_is_captured(self):
        """The premise: capture works, so a refusal below means something."""
        mismatches = self._selected("MISMATCH ")
        self.assertNotEqual(mismatches, [])
        document = create_baseline(self.context, mismatches[:1])
        self.assertEqual(len(document["violations"]), 1)
        self.assertEqual(document["violations"][0]["kind"], "component-facet")

    def test_every_refused_class_has_a_witness_the_product_emitted(self):
        """A renamed emitter empties a class here instead of passing silently."""
        for label, prefix in UNBASELINABLE_PREFIXES.items():
            with self.subTest(diagnostic=label):
                self.assertNotEqual(
                    self.witnesses[label],
                    [],
                    f"no live run produced a {prefix!r} diagnostic",
                )
                if label in OBSERVED_WITNESSES:
                    self.assertIn(OBSERVED_WITNESSES[label], self.witnesses[label])

    def test_the_current_digest_witness_names_its_component_and_provider(self):
        """Only the head is pinned: the tail is CPython's JSON parser talking."""
        self.assertTrue(
            any(
                issue.startswith(
                    "CURRENT DIGEST ERROR svc: OpenAPI canonicalization failed "
                    "for api.json: JSON parse failed for api.json: "
                )
                for issue in self.witnesses["current digest computation"]
            ),
            self.witnesses["current digest computation"],
        )

    def test_every_collected_witness_is_refused_by_name(self):
        for label in UNBASELINABLE_PREFIXES:
            for issue in self.witnesses[label]:
                with self.subTest(diagnostic=label, issue=issue[:50]):
                    with self.assertRaises(BaselineError) as caught:
                        create_baseline(self.context, [issue])
                    self.assertEqual(
                        str(caught.exception),
                        "cannot baseline integrity, configuration, or "
                        "unclassified issue: " + issue,
                    )

    def test_a_shape_no_emitter_produces_is_refused_as_unclassified(self):
        """The default-deny arm, stated so the table above is not mistaken for it."""
        self.assertIsNone(violation_identity(UNCLASSIFIED_ISSUE))
        with self.assertRaises(BaselineError) as caught:
            create_baseline(self.context, [UNCLASSIFIED_ISSUE])
        self.assertEqual(
            str(caught.exception),
            "cannot baseline integrity, configuration, or unclassified issue: "
            + UNCLASSIFIED_ISSUE,
        )

    def test_the_real_drift_lines_do_produce_identities(self):
        """The positive side: refusal is an answer, not the only answer."""
        mismatches = self._selected("MISMATCH ")
        slice_mismatches = self._selected("SLICE MISMATCH ")
        self.assertNotEqual(mismatches, [])
        self.assertNotEqual(slice_mismatches, [])
        for issue in mismatches:
            with self.subTest(issue=issue[:40]):
                identity = violation_identity(issue)
                self.assertIsNotNone(identity, issue)
                subject, facet = issue[len("MISMATCH "):].split(":", 1)[0].rsplit(
                    ".", 1
                )
                self.assertEqual(identity["kind"], "component-facet")
                self.assertEqual(identity["subject"], subject)
                self.assertEqual(identity["facet"], facet)
        for issue in slice_mismatches:
            with self.subTest(issue=issue[:40]):
                identity = violation_identity(issue)
                self.assertIsNotNone(identity, issue)
                subject, facet = issue[len("SLICE MISMATCH "):].split(
                    ":", 1
                )[0].rsplit(".", 1)
                self.assertEqual(identity["kind"], "slice-facet")
                self.assertEqual(identity["subject"], subject)
                self.assertEqual(identity["facet"], facet)

    def test_a_consumer_line_is_baselinable_only_behind_its_own_mismatch(self):
        consumers = self._selected("AFFECTED CONSUMERS ")
        self.assertNotEqual(consumers, [])
        owning = next(
            issue for issue in self.observed if issue.startswith("MISMATCH svc.")
        )
        document = create_baseline(self.context, [owning, consumers[0]])
        self.assertEqual(
            sorted(entry["kind"] for entry in document["violations"]),
            ["affected-consumers", "component-facet"],
        )
        with self.assertRaises(BaselineError) as caught:
            create_baseline(self.context, [consumers[0]])
        self.assertEqual(
            str(caught.exception),
            "cannot baseline integrity, configuration, or unclassified issue: "
            + consumers[0],
        )

    def test_ancillary_version_metadata_rides_on_its_compat_mismatch(self):
        """The one exception the `METADATA MISMATCH` rows must not overstate."""
        compat = next(
            issue
            for issue in self.observed
            if issue.startswith("MISMATCH svc.compat:")
        )
        version = next(
            issue
            for issue in self.observed
            if issue.startswith("METADATA MISMATCH svc.version:")
        )
        with self.assertRaises(BaselineError):
            create_baseline(self.context, [version])
        document = create_baseline(self.context, [compat, version])
        self.assertEqual(
            [(entry["kind"], entry["facet"]) for entry in document["violations"]],
            [("component-facet", "compat")],
        )

    def test_an_unbaselinable_issue_poisons_a_capture_that_also_has_drift(self):
        unavailable = self.witnesses["unavailable facet"][0]
        mismatch = self._selected("MISMATCH ")[0]
        with self.assertRaises(BaselineError) as caught:
            create_baseline(self.context, [mismatch, unavailable])
        self.assertEqual(
            str(caught.exception),
            "cannot baseline integrity, configuration, or unclassified issue: "
            + unavailable,
        )


if __name__ == "__main__":  # pragma: no cover - convenience for local runs
    unittest.main()
