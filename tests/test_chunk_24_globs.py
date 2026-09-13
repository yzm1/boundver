"""Six promises about which edits move a digest, which selectors reach a file,
and which failures a scoped repair is allowed to touch.

The config digest is the load-bearing one. `spec/HASHING.md` names the fields
the semantic view covers and then names five things it deliberately ignores,
and a test that samples a few of each cannot tell you that a field newly added
to the contract landed on the right side of that line. So the surface here is
read at runtime from the field constants in `boundver._config_contract` - the
same frozensets `_reject_unknown_fields` uses to decide what a user is allowed
to write - and every one of the thirty-three declared fields must carry at
least one row in the mutation table. A field added to the contract tomorrow
with no row fails the check rather than being silently untested. The second
derivation is sharper: the set of fields whose every row is digest-invariant is
compared against the four the spec calls presentation-only, so a field that
quietly fell out of `_semantic_config` cannot pass by having only invariance
rows written for it. Two premise tests sit under the table, because an
invariance row is exactly the shape of assertion that passes when the mutation
did nothing at all: one proves every row really edits the serialized config,
and the must-change half proves the digest is sensitive to begin with.

The glob obligations needed a probe before a line was written. A segment that
merely contains `**` compiles to a single-segment `glob` part rather than a
`recursive` one, which sounds like an implementation detail until you follow it
to a declaration: `api/**.yaml` selects `api/a.yaml` and silently omits
`api/v1/b.yaml`, validation reports no coverage failure, and generation
succeeds over a boundary that is missing a contract file. That is asserted here
three ways - on the compiled part kinds, on the matcher, and on the digest a
real declaration produces, which is shown equal to the explicit root-only list
and unequal to the recursive one. The same fixture carries the source-view
half of the coverage obligation: a file committed to HEAD and then deleted from
the working tree makes `--source head` and `--source index` report an uncovered
boundary artifact while `--source working-tree` reports nothing, and the
boundary digest moves with the validation verdict rather than against it, which
is what "validation must inspect the same tracked source as hashing" has to
mean if it means anything.

Two obligations here are cheap to state and were simply never driven. The
SemVer grammar is hand-written rather than borrowed, so each of its rules is a
line of code that could be deleted without any caller noticing: leading zeroes
in a core identifier, leading zeroes in a numeric prerelease identifier, empty
prerelease and build segments, a second `+` in build metadata, and an uppercase
`V`. Every rejection has to come back as the input verbatim, because that third
slot is what a diagnostic prints, and every rejected input in the table below
differs from an accepted one by a single character so the rejection is evidence
about the rule and not about a parser that refuses everything. The scoped
`--update` obligation needed the opposite kind of work: one fixture, four
different ways to break it, and both halves of the pair run against each. The
exit code is where the appended diagnostic becomes visible - a project rename
carries none of the prefixes `_drift_exit_code` treats as a safety issue and
exits 1 on its own, and the same repository with `--components` and `--update`
exits 2 because the appended line begins with `LOCKFILE`.

The budget obligations are stated over an unbounded space, so they are
Hypothesis properties with a segment-model oracle written out in this file -
fnmatch decides one segment, and only a whole `**` token may span more or fewer
than one. The expensive part was not the property but its premise. Asserting
that a tight budget never degrades a match to False proves nothing unless some
budget in the sweep actually raised, so the threshold is measured rather than
guessed: a generous operation is run once, its `steps` counter read, and every
budget below that number is required to raise while every budget at or above it
is required to return the true answer. The cache-charging invariant needed the
same care - "the second call charged something" would pass if the pattern were
recompiled - so it is paired with the observation that the second call charges
strictly less than the first and leaves `compiled_patterns` at one.

Covers OBL-GLOBS-006, OBL-GLOBS-013, OBL-GLOBS-014, OBL-GLOBS-015,
OBL-GLOBS-018 and OBL-GLOBS-021.
"""

from __future__ import annotations

import fnmatch
import json
import unittest
from typing import Any, Callable, Dict, List, Set, Tuple

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    invariant,
    rule,
    run_state_machine_as_test,
)

from boundver._config import (
    _expand_component_paths,
    config_warnings,
    validate_config,
)
from boundver._config_contract import (
    BEHAVIOR_FIELDS,
    BOUNDARY_FIELDS,
    COMPONENT_FIELDS,
    COVERAGE_EXCLUSION_FIELDS,
    COVERAGE_FIELDS,
    DEFAULT_FIELDS,
    DERIVATION_FIELDS,
    PROVIDER_FIELDS,
    ROOT_FIELDS,
    SLICE_FIELDS,
    VERSION_SOURCE_FIELDS,
)
from boundver._git import _capture_git_source_snapshot
from boundver._lockfile import SEMANTIC_CONFIG_VERSION, semantic_config_digest
from boundver._utils import (
    GuardrailError,
    _PathGlobOperation,
    _compile_path_glob,
    _match_path_glob,
)
from boundver.versions import parse_semver

from tests._parity import run_cli
from tests._scenarios import Scenario


# ---------------------------------------------------------------------------
# OBL-GLOBS-006 - which configuration edits move config_digest
# ---------------------------------------------------------------------------


def _semantic_base() -> Dict[str, Any]:
    """A configuration that spells every field the contract declares.

    Every field in the runtime surface below has to be present with a value
    that a mutation can move, otherwise a "changes the digest" row would be
    testing the difference between absent and present rather than the
    difference the obligation names.
    """
    return {
        "$schema": "https://example.invalid/boundary.schema.json",
        "project": "scenario",
        "providers": [
            {"module": "vendor.one", "class": "One", "name": "one"},
            {"module": "vendor.two", "class": "Two", "name": "two"},
        ],
        "defaults": {"compat_mode": "major", "verify_facets": ["boundary", "exact"]},
        "coverage": {
            "source_indicators": ["**/*.py", "**/*.ts"],
            "exclusions": [
                {
                    "paths": ["generated/**"],
                    "facets": ["ownership", "boundary"],
                    "reason": "generated source",
                }
            ],
        },
        "derivations": {
            "api": {
                "inputs": ["infra/template.yaml", "infra/routes.yaml"],
                "outputs": [
                    "services/svc/api.json",
                    "services/svc/other.json",
                ],
                "evidence": "infra/api.boundver-derivation.json",
                "generator": "fixture-generator/v1",
            },
            "docs": {
                "inputs": ["docs/source.md"],
                "outputs": ["services/svc/docs.json"],
                "evidence": "docs/site.boundver-derivation.json",
                "generator": "docs-generator/v1",
            }
        },
        "components": {
            "svc": {
                "path": "services/svc",
                "ecosystem": "python",
                "note": "a component annotation",
                "version_source": {"file": "version.txt", "field": "version"},
                "boundary": {
                    "provider": "path-hash",
                    "paths": ["api/a.yaml", "api/b.yaml"],
                    "options": {"strict": True},
                    "note": "a boundary annotation",
                },
                "behavior": {"paths": ["api", "lib"]},
                "vendored_copies": ["vendor/a", "vendor/b"],
                "consumers": ["cli", "web"],
                "external_consumers": ["partner-a", "partner-b"],
                "verify_facets": ["behavior", "boundary"],
            },
            "cli": {
                "path": "apps/cli",
                "version_source": {"git_tag_prefix": "cli-v"},
                "boundary": {"provider": "leaf", "paths": []},
                "behavior": {"paths": []},
            },
        },
        "slices": {
            "public": {
                "description": "",
                "mode": "exact",
                "components": ["cli", "svc"],
            }
        },
    }


def _svc(config: Dict[str, Any]) -> Dict[str, Any]:
    return config["components"]["svc"]


def _reverse_keys_in_place(target: Dict[str, Any]) -> None:
    """Reverse one object's insertion order without changing its content."""
    items = list(target.items())
    for key in list(target):
        del target[key]
    for key, value in reversed(items):
        target[key] = value


#: Every mutation of a declared configuration field, keyed by the dotted name
#: the surface derivation below produces, and tagged with the digest response
#: the obligation requires: "same" for a presentation-only field, a set-like
#: reordering, or an omitted documented default, and "changed" for everything
#: the spec enumerates as covered.
FIELD_MUTATIONS: Dict[str, Tuple[Tuple[str, Callable[[Dict[str, Any]], Any], str], ...]] = {
    "$schema": (
        ("replaced", lambda c: c.__setitem__("$schema", "https://other.invalid/x.json"), "same"),
        ("removed", lambda c: c.pop("$schema"), "same"),
    ),
    "project": (
        ("renamed", lambda c: c.__setitem__("project", "renamed"), "changed"),
    ),
    "providers": (
        ("declaration order reversed", lambda c: c.__setitem__("providers", list(reversed(c["providers"]))), "changed"),
        ("removed", lambda c: c.pop("providers"), "changed"),
    ),
    "defaults": (
        ("emptied", lambda c: c.__setitem__("defaults", {}), "changed"),
    ),
    "coverage": (
        ("removed", lambda c: c.pop("coverage"), "same"),
    ),
    "components": (
        ("member added", lambda c: c["components"].__setitem__(
            "extra", {"path": "apps/extra", "boundary": {"provider": "leaf", "paths": []}}
        ), "changed"),
        ("member insertion order reversed", lambda c: c.__setitem__(
            "components", dict(reversed(list(c["components"].items())))
        ), "same"),
    ),
    "slices": (
        ("member added", lambda c: c["slices"].__setitem__(
            "internal", {"mode": "exact", "components": ["cli"]}
        ), "changed"),
    ),
    "defaults.compat_mode": (
        ("omitted rather than materialized as major", lambda c: c["defaults"].pop("compat_mode"), "same"),
        ("set to minor", lambda c: c["defaults"].__setitem__("compat_mode", "minor"), "changed"),
    ),
    "defaults.verify_facets": (
        ("reordered", lambda c: c["defaults"].__setitem__("verify_facets", ["exact", "boundary"]), "same"),
        ("narrowed", lambda c: c["defaults"].__setitem__("verify_facets", ["exact"]), "changed"),
    ),
    "coverage.source_indicators": (
        (
            "narrowed",
            lambda c: c["coverage"].__setitem__("source_indicators", ["**/*.py"]),
            "same",
        ),
    ),
    "coverage.exclusions": (
        ("removed", lambda c: c["coverage"].pop("exclusions"), "same"),
    ),
    "coverage.exclusions[].paths": (
        (
            "repointed",
            lambda c: c["coverage"]["exclusions"][0].__setitem__(
                "paths", ["build/**"]
            ),
            "same",
        ),
    ),
    "coverage.exclusions[].facets": (
        (
            "narrowed",
            lambda c: c["coverage"]["exclusions"][0].__setitem__(
                "facets", ["ownership"]
            ),
            "same",
        ),
    ),
    "coverage.exclusions[].reason": (
        (
            "rewritten",
            lambda c: c["coverage"]["exclusions"][0].__setitem__(
                "reason", "different rationale"
            ),
            "same",
        ),
    ),
    "derivations": (
        ("removed", lambda c: c.pop("derivations"), "changed"),
    ),
    "derivations[].inputs": (
        (
            "reordered",
            lambda c: c["derivations"]["api"].__setitem__(
                "inputs", ["infra/routes.yaml", "infra/template.yaml"]
            ),
            "same",
        ),
        (
            "repointed",
            lambda c: c["derivations"]["api"].__setitem__(
                "inputs", ["infra/other.yaml"]
            ),
            "changed",
        ),
    ),
    "derivations[].outputs": (
        (
            "reordered",
            lambda c: c["derivations"]["api"].__setitem__(
                "outputs", ["services/svc/other.json", "services/svc/api.json"]
            ),
            "same",
        ),
        (
            "repointed",
            lambda c: c["derivations"]["api"].__setitem__(
                "outputs",
                ["services/svc/api.json", "services/svc/changed.json"],
            ),
            "changed",
        ),
    ),
    "derivations[].evidence": (
        (
            "repointed",
            lambda c: c["derivations"]["api"].__setitem__(
                "evidence", "infra/other.boundver-derivation.json"
            ),
            "changed",
        ),
    ),
    "derivations[].generator": (
        (
            "changed",
            lambda c: c["derivations"]["api"].__setitem__(
                "generator", "fixture-generator/v2"
            ),
            "changed",
        ),
    ),
    "providers[].module": (
        ("retargeted", lambda c: c["providers"][0].__setitem__("module", "vendor.other"), "changed"),
    ),
    "providers[].class": (
        ("retargeted", lambda c: c["providers"][0].__setitem__("class", "Other"), "changed"),
    ),
    "providers[].name": (
        ("renamed", lambda c: c["providers"][0].__setitem__("name", "other"), "changed"),
    ),
    "components[].path": (
        ("moved", lambda c: _svc(c).__setitem__("path", "services/other"), "changed"),
    ),
    "components[].ecosystem": (
        ("changed", lambda c: _svc(c).__setitem__("ecosystem", "npm"), "same"),
        ("removed", lambda c: _svc(c).pop("ecosystem"), "same"),
    ),
    "components[].note": (
        ("changed", lambda c: _svc(c).__setitem__("note", "a different annotation"), "same"),
        ("removed", lambda c: _svc(c).pop("note"), "same"),
    ),
    "components[].version_source": (
        ("removed", lambda c: _svc(c).pop("version_source"), "changed"),
    ),
    "components[].version_source.file": (
        ("repointed", lambda c: _svc(c)["version_source"].__setitem__("file", "other.txt"), "changed"),
    ),
    "components[].version_source.field": (
        ("repointed", lambda c: _svc(c)["version_source"].__setitem__("field", "release"), "changed"),
    ),
    "components[].version_source.git_tag_prefix": (
        ("repointed", lambda c: c["components"]["cli"]["version_source"].__setitem__(
            "git_tag_prefix", "cli-r"
        ), "changed"),
    ),
    "components[].version_source.component": (
        (
            "repointed",
            lambda c: c["components"].__setitem__(
                "inherited",
                {
                    "path": "apps/inherited",
                    "version_source": {"component": "svc"},
                    "boundary": {"provider": "leaf", "paths": []},
                },
            ),
            "changed",
        ),
    ),
    "components[].version_source.constant": (
        (
            "declared",
            lambda c: c["components"].__setitem__(
                "constant",
                {
                    "path": "apps/constant",
                    "version_source": {"constant": "1.2.3"},
                    "boundary": {"provider": "leaf", "paths": []},
                },
            ),
            "changed",
        ),
    ),
    "components[].boundary": (
        ("removed", lambda c: _svc(c).pop("boundary"), "changed"),
    ),
    "components[].boundary.provider": (
        ("swapped", lambda c: _svc(c)["boundary"].__setitem__("provider", "json-file"), "changed"),
    ),
    "components[].boundary.paths": (
        ("reordered", lambda c: _svc(c)["boundary"].__setitem__(
            "paths", ["api/b.yaml", "api/a.yaml"]
        ), "same"),
        ("widened", lambda c: _svc(c)["boundary"].__setitem__(
            "paths", ["api/a.yaml", "api/c.yaml"]
        ), "changed"),
    ),
    "components[].boundary.options": (
        ("flipped", lambda c: _svc(c)["boundary"].__setitem__("options", {"strict": False}), "changed"),
        ("removed", lambda c: _svc(c)["boundary"].pop("options"), "changed"),
    ),
    "components[].boundary.note": (
        ("changed", lambda c: _svc(c)["boundary"].__setitem__("note", "a different annotation"), "same"),
        ("removed", lambda c: _svc(c)["boundary"].pop("note"), "same"),
    ),
    "components[].behavior": (
        ("removed", lambda c: _svc(c).pop("behavior"), "changed"),
    ),
    "components[].behavior.paths": (
        ("reordered", lambda c: _svc(c)["behavior"].__setitem__("paths", ["lib", "api"]), "same"),
        ("narrowed", lambda c: _svc(c)["behavior"].__setitem__("paths", ["api"]), "changed"),
    ),
    "components[].vendored_copies": (
        ("reordered", lambda c: _svc(c).__setitem__("vendored_copies", ["vendor/b", "vendor/a"]), "same"),
        ("repointed", lambda c: _svc(c).__setitem__("vendored_copies", ["vendor/a", "vendor/c"]), "changed"),
    ),
    "components[].consumers": (
        ("reordered", lambda c: _svc(c).__setitem__("consumers", ["web", "cli"]), "same"),
        ("narrowed", lambda c: _svc(c).__setitem__("consumers", ["cli"]), "changed"),
    ),
    "components[].external_consumers": (
        ("reordered", lambda c: _svc(c).__setitem__(
            "external_consumers", ["partner-b", "partner-a"]
        ), "same"),
        ("narrowed", lambda c: _svc(c).__setitem__("external_consumers", ["partner-a"]), "changed"),
    ),
    "components[].verify_facets": (
        ("reordered", lambda c: _svc(c).__setitem__("verify_facets", ["boundary", "behavior"]), "same"),
        ("narrowed", lambda c: _svc(c).__setitem__("verify_facets", ["exact"]), "changed"),
    ),
    "slices[].description": (
        ("omitted rather than materialized as empty", lambda c: c["slices"]["public"].pop("description"), "same"),
        ("written", lambda c: c["slices"]["public"].__setitem__("description", "the public API"), "same"),
    ),
    "slices[].mode": (
        ("omitted rather than materialized as exact", lambda c: c["slices"]["public"].pop("mode"), "same"),
        ("relaxed", lambda c: c["slices"]["public"].__setitem__("mode", "at-least"), "changed"),
    ),
    "slices[].components": (
        ("reordered", lambda c: c["slices"]["public"].__setitem__("components", ["svc", "cli"]), "same"),
        ("narrowed", lambda c: c["slices"]["public"].__setitem__("components", ["svc"]), "changed"),
    ),
    "slices[].closure_of": (
        ("declared", lambda c: c["slices"]["public"].__setitem__("closure_of", "svc"), "changed"),
    ),
}

#: Fields that `spec/HASHING.md` and the declaration-coverage contract exclude
#: from `config_digest`. Object insertion order is asserted separately.
DIGEST_NEUTRAL_FIELDS = frozenset(
    {
        "$schema",
        "coverage",
        "coverage.source_indicators",
        "coverage.exclusions",
        "coverage.exclusions[].paths",
        "coverage.exclusions[].facets",
        "coverage.exclusions[].reason",
        "components[].ecosystem",
        "components[].note",
        "components[].boundary.note",
        "slices[].description",
    }
)

#: Each object whose insertion order must be invisible, named by how to reach
#: it from a fresh base configuration.
ORDERED_OBJECTS: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    "root": lambda c: c,
    "defaults": lambda c: c["defaults"],
    "coverage": lambda c: c["coverage"],
    "a coverage exclusion": lambda c: c["coverage"]["exclusions"][0],
    "derivations": lambda c: c["derivations"],
    "a derivation": lambda c: c["derivations"]["api"],
    "one component": lambda c: _svc(c),
    "a boundary declaration": lambda c: _svc(c)["boundary"],
    "a slice declaration": lambda c: c["slices"]["public"],
}


def _declared_configuration_surface() -> Set[str]:
    """Every field a user may write, read from the contract at runtime."""
    surface = set(ROOT_FIELDS)
    surface |= {f"defaults.{name}" for name in DEFAULT_FIELDS}
    surface |= {f"coverage.{name}" for name in COVERAGE_FIELDS}
    surface |= {
        f"coverage.exclusions[].{name}" for name in COVERAGE_EXCLUSION_FIELDS
    }
    surface |= {f"derivations[].{name}" for name in DERIVATION_FIELDS}
    surface |= {f"providers[].{name}" for name in PROVIDER_FIELDS}
    surface |= {f"components[].{name}" for name in COMPONENT_FIELDS}
    surface |= {f"components[].boundary.{name}" for name in BOUNDARY_FIELDS}
    surface |= {f"components[].behavior.{name}" for name in BEHAVIOR_FIELDS}
    surface |= {
        f"components[].version_source.{name}" for name in VERSION_SOURCE_FIELDS
    }
    surface |= {f"slices[].{name}" for name in SLICE_FIELDS}
    return surface


class SemanticConfigDigestSurfaceTests(unittest.TestCase):
    """OBL-GLOBS-006: the whole declared surface, on the side the spec puts it."""

    def _mutated(self, mutate: Callable[[Dict[str, Any]], Any]) -> Dict[str, Any]:
        config = _semantic_base()
        mutate(config)
        return config

    def test_every_declared_configuration_field_carries_a_mutation_row(self):
        """The surface is read from the contract, not listed here by hand.

        `_reject_unknown_fields` decides what a user may write from these
        frozensets, so a field added to the contract without a row below
        fails this rather than being quietly untested.
        """
        self.assertEqual(set(FIELD_MUTATIONS), _declared_configuration_surface())

    def test_only_the_documented_digest_neutral_fields_are_invisible(self):
        """Derived, so a field that fell out of the semantic view is caught.

        A field whose every row is invariant is a field the digest cannot
        see. The spec names presentation fields and the read-only coverage
        policy; any other invariant field is a hole through which a boundary
        could be widened with no digest movement.
        """
        invisible = {
            field
            for field, rows in FIELD_MUTATIONS.items()
            if all(expectation == "same" for _, _, expectation in rows)
        }
        self.assertEqual(invisible, set(DIGEST_NEUTRAL_FIELDS))

    def test_every_row_actually_edits_the_configuration(self):
        """The premise under every invariance row in the table.

        A mutation that did nothing would satisfy "the digest did not move"
        perfectly. Comparing the serialized configuration without sorting
        keys catches both a no-op edit and a reordering that reordered
        nothing.
        """
        base = json.dumps(_semantic_base())
        for field, rows in FIELD_MUTATIONS.items():
            for label, mutate, _ in rows:
                with self.subTest(field=field, row=label):
                    self.assertNotEqual(json.dumps(self._mutated(mutate)), base)

    def test_each_row_moves_the_digest_exactly_when_the_spec_says_it_must(self):
        """The table itself: fifty-one edits against one reference digest."""
        reference = semantic_config_digest(_semantic_base())
        for field, rows in FIELD_MUTATIONS.items():
            for label, mutate, expectation in rows:
                with self.subTest(field=field, row=label):
                    digest = semantic_config_digest(self._mutated(mutate))
                    if expectation == "same":
                        self.assertEqual(digest, reference, f"{field}: {label}")
                    else:
                        self.assertNotEqual(digest, reference, f"{field}: {label}")

    def test_an_omitted_documented_default_hashes_as_its_materialized_spelling(self):
        """The three defaults `_semantic_config` materializes, spelled both ways.

        The base configuration writes `compat_mode`, slice `mode` and slice
        `description` out in full, so each row here is the omitted spelling
        judged against the written one rather than against itself. The
        assertion that the key really left the object is what stops a
        mutation that popped nothing from passing.
        """
        reference = semantic_config_digest(_semantic_base())
        materialized: Dict[str, Tuple[Callable[[Dict[str, Any]], Dict[str, Any]], str, Any]] = {
            "defaults.compat_mode materializes as major": (
                lambda c: c["defaults"],
                "compat_mode",
                "major",
            ),
            "slice mode materializes as exact": (
                lambda c: c["slices"]["public"],
                "mode",
                "exact",
            ),
            "slice description materializes as the empty string": (
                lambda c: c["slices"]["public"],
                "description",
                "",
            ),
        }
        for label, (reach, key, spelling) in materialized.items():
            with self.subTest(default=label):
                config = _semantic_base()
                self.assertEqual(reach(config)[key], spelling, label)
                del reach(config)[key]
                self.assertNotIn(key, reach(config))
                self.assertEqual(semantic_config_digest(config), reference, label)

    def test_the_generated_lock_carries_the_semantic_digest_of_its_own_config(self):
        """The binding between the function tested above and the lock field.

        Everything else in this class calls `semantic_config_digest`
        directly. `config_digest` is the field the obligation names, so the
        two are shown to be the same number on a real repository, and then
        one edit from each side of the table is driven through generation.
        """
        with Scenario() as scene:
            scene.component(
                "svc", path="services/svc", boundary=["api/a.yaml"], behavior=["api"]
            )
            scene.file("services/svc/api/a.yaml", "openapi: 3.1.0\n")
            scene.commit()
            lockfile = scene.generate(source="head")
            self.assertEqual(
                lockfile["config_digest"], semantic_config_digest(scene.config)
            )
            self.assertEqual(lockfile["config_contract"], SEMANTIC_CONFIG_VERSION)
            _svc(scene.config)["note"] = "a cosmetic annotation"
            self.assertEqual(
                scene.generate(source="head")["config_digest"],
                lockfile["config_digest"],
            )
            _svc(scene.config)["boundary"]["provider"] = "json-file"
            self.assertNotEqual(
                scene.generate(source="head")["config_digest"],
                lockfile["config_digest"],
            )

    def test_object_insertion_order_is_invisible_at_every_nesting_level(self):
        """The fifth documented invariance, which is not a field.

        Reversing a dict in place changes the serialized bytes and nothing
        else, and the assertion that the bytes moved is what stops this from
        passing on an object with fewer than two keys.
        """
        reference = semantic_config_digest(_semantic_base())
        for label, reach in ORDERED_OBJECTS.items():
            with self.subTest(object=label):
                config = _semantic_base()
                before = json.dumps(config)
                _reverse_keys_in_place(reach(config))
                self.assertNotEqual(json.dumps(config), before, label)
                self.assertEqual(semantic_config_digest(config), reference, label)


# ---------------------------------------------------------------------------
# OBL-GLOBS-013 - behavior.paths against the source view used for hashing
# ---------------------------------------------------------------------------

#: The uncovered-file preview appears twice in `_config.py`: once as a hard
#: error inside `validate_config` and once as a warning in `config_warnings`.
#: Both truncate at three names, so both are checked against the same fixture.
COVERAGE_ERROR_PREFIX = "Component 'svc' behavior.paths must cover every boundary artifact; uncovered: "
COVERAGE_WARNING_PREFIX = "Component 'svc' behavior.paths does not currently cover boundary files: "
COVERAGE_WARNING_SUFFIX = " — behavior should usually be a superset of boundary"


def _coverage_errors(scene: Scenario, source: str) -> List[str]:
    return [
        error
        for error in validate_config(scene.config, scene.root, source=source)
        if "behavior.paths" in error
    ]


class BehaviorCoverageTests(unittest.TestCase):
    """OBL-GLOBS-013: the coverage check, its preview, and its source view."""

    def test_a_directory_prefix_boundary_names_three_files_and_counts_the_rest(self):
        """Four uncovered files, three named, one counted.

        A literal that is a directory prefix expands to every file beneath
        it in both the validation check and the hashing expansion, which is
        what makes four uncovered files reachable from a one-element
        declaration at all.
        """
        with Scenario() as scene:
            scene.component(
                "svc", path="services/svc", boundary=["api"], behavior=["README.md"]
            )
            for name in ("a", "b", "c", "d"):
                scene.file(f"services/svc/api/{name}.yaml", "openapi: 3.1.0\n")
            scene.file("services/svc/README.md", "docs\n")
            scene.commit()
            expected = COVERAGE_ERROR_PREFIX + "api/a.yaml, api/b.yaml, api/c.yaml, +1 more"
            for source in ("head", "index", "working-tree"):
                with self.subTest(source=source):
                    self.assertEqual(_coverage_errors(scene, source), [expected])
            self.assertIn(
                COVERAGE_WARNING_PREFIX
                + "api/a.yaml, api/b.yaml, api/c.yaml, +1 more"
                + COVERAGE_WARNING_SUFFIX,
                config_warnings(scene.config, scene.root),
            )

    def test_three_uncovered_files_are_named_without_a_count(self):
        """The premise under the truncation above.

        `+1 more` is only evidence of truncation if the same code path
        prints no count when there is nothing to truncate.
        """
        with Scenario() as scene:
            scene.component(
                "svc", path="services/svc", boundary=["api"], behavior=["README.md"]
            )
            for name in ("a", "b", "c"):
                scene.file(f"services/svc/api/{name}.yaml", "openapi: 3.1.0\n")
            scene.file("services/svc/README.md", "docs\n")
            scene.commit()
            self.assertEqual(
                _coverage_errors(scene, "head"),
                [COVERAGE_ERROR_PREFIX + "api/a.yaml, api/b.yaml, api/c.yaml"],
            )

    def test_a_glob_boundary_is_judged_against_a_glob_behavior(self):
        """Both sides wildcards, expanded by the same operation.

        The suite's existing coverage failures are literal against literal,
        which never reaches `_PathGlobOperation` on either side.
        """
        with Scenario() as scene:
            scene.component(
                "svc",
                path="services/svc",
                boundary=["api/*.yaml"],
                behavior=["api/*.json"],
            )
            scene.file("services/svc/api/a.yaml", "openapi: 3.1.0\n")
            scene.file("services/svc/api/a.json", "{}\n")
            scene.commit()
            self.assertEqual(
                _coverage_errors(scene, "head"),
                [COVERAGE_ERROR_PREFIX + "api/a.yaml"],
            )

    def test_a_recursive_glob_boundary_is_covered_by_a_directory_prefix_behavior(self):
        """The passing half: the two selector kinds must expand alike.

        Without this the failures above could be explained by a glob that
        selects nothing rather than by a coverage hole.
        """
        with Scenario() as scene:
            scene.component(
                "svc",
                path="services/svc",
                boundary=["api/**/*.yaml"],
                behavior=["api"],
            )
            scene.file("services/svc/api/v1/a.yaml", "openapi: 3.1.0\n")
            scene.file("services/svc/api/v1/deep/b.yaml", "openapi: 3.1.1\n")
            scene.commit()
            for source in ("head", "index", "working-tree"):
                with self.subTest(source=source):
                    self.assertEqual(_coverage_errors(scene, source), [])
            self.assertEqual(
                sorted(
                    _expand_component_paths(
                        scene.root, "services/svc", ["api/**/*.yaml"], source="head"
                    )
                ),
                ["api/v1/a.yaml", "api/v1/deep/b.yaml"],
            )

    def test_a_committed_file_deleted_on_disk_still_fails_head_and_index(self):
        """The obligation's whole point, and the one case that separates the views.

        `api/b.yaml` is in HEAD and in the index and gone from the working
        tree. If validation expanded against the working tree it would see a
        covered boundary and let a lock ship whose HEAD-level boundary
        digest includes a file no behavior selector reaches.
        """
        with Scenario() as scene:
            scene.component(
                "svc",
                path="services/svc",
                boundary=["api/*.yaml"],
                behavior=["api/a.yaml"],
            )
            scene.file("services/svc/api/a.yaml", "openapi: 3.1.0\n")
            scene.file("services/svc/api/b.yaml", "openapi: 3.1.1\n")
            scene.commit()
            scene.remove("services/svc/api/b.yaml")
            expected = COVERAGE_ERROR_PREFIX + "api/b.yaml"
            for source in ("head", "index"):
                with self.subTest(source=source):
                    self.assertEqual(_coverage_errors(scene, source), [expected])
                    self.assertEqual(
                        sorted(
                            _expand_component_paths(
                                scene.root, "services/svc", ["api/*.yaml"], source=source
                            )
                        ),
                        ["api/a.yaml", "api/b.yaml"],
                    )
            self.assertEqual(_coverage_errors(scene, "working-tree"), [])
            self.assertEqual(
                sorted(
                    _expand_component_paths(
                        scene.root, "services/svc", ["api/*.yaml"], source="working-tree"
                    )
                ),
                ["api/a.yaml"],
            )

    def test_the_expansion_validation_checks_is_the_expansion_hashing_uses(self):
        """The comment's claim, judged on the digest rather than on the comment.

        The same declaration is hashed under each source and compared with
        an explicit list of the files that source sees. A validation check
        that read a different view than the hash would break one of the two
        equalities below.
        """

        def build(boundary: List[str], deleted: bool) -> Scenario:
            scene = Scenario()
            scene.component(
                "svc", path="services/svc", boundary=boundary, behavior=["api"]
            )
            scene.file("services/svc/api/a.yaml", "openapi: 3.1.0\n")
            scene.file("services/svc/api/b.yaml", "openapi: 3.1.1\n")
            scene.commit()
            if deleted:
                scene.remove("services/svc/api/b.yaml")
            return scene

        with build(["api/a.yaml", "api/b.yaml"], deleted=False) as scene:
            both_files = scene.digest("svc", "boundary", source="head")
        with build(["api/a.yaml"], deleted=True) as scene:
            one_file = scene.digest("svc", "boundary", source="working-tree")
        self.assertNotEqual(both_files, one_file)
        with build(["api/*.yaml"], deleted=True) as scene:
            self.assertEqual(scene.digest("svc", "boundary", source="head"), both_files)
            self.assertEqual(scene.digest("svc", "boundary", source="index"), both_files)
            self.assertEqual(
                scene.digest("svc", "boundary", source="working-tree"), one_file
            )

    def test_a_captured_snapshot_gives_the_check_the_same_verdict_as_the_live_source(self):
        """The path the CLI actually takes, which reads no working tree at all.

        `verify` and `generate` capture a snapshot once and hand it to both
        validation and hashing, so `_expand_component_paths` takes its
        `_snapshot_files` branch rather than shelling out per component. A
        snapshot exists only for head and index; `working-tree` has no
        captured form, which is why it is absent here and present above.
        """
        with Scenario() as scene:
            scene.component(
                "svc",
                path="services/svc",
                boundary=["api/*.yaml"],
                behavior=["api/a.yaml"],
            )
            scene.file("services/svc/api/a.yaml", "openapi: 3.1.0\n")
            scene.file("services/svc/api/b.yaml", "openapi: 3.1.1\n")
            scene.commit()
            scene.remove("services/svc/api/b.yaml")
            expected = [COVERAGE_ERROR_PREFIX + "api/b.yaml"]
            for source in ("head", "index"):
                with self.subTest(source=source):
                    snapshot = _capture_git_source_snapshot(scene.root, source)
                    captured = [
                        error
                        for error in validate_config(
                            scene.config,
                            scene.root,
                            source=source,
                            snapshot=snapshot,
                        )
                        if "behavior.paths" in error
                    ]
                    self.assertEqual(captured, expected)
                    self.assertEqual(captured, _coverage_errors(scene, source))
            with self.assertRaises(ValueError):
                _capture_git_source_snapshot(scene.root, "working-tree")

    def test_an_untracked_file_is_invisible_to_the_check_under_every_source(self):
        """The other direction the comment names: no view is the raw filesystem.

        A file written and never staged must not make a valid configuration
        fail, including under `--source working-tree`, whose listing is
        still the tracked one.
        """
        with Scenario() as scene:
            scene.component(
                "svc",
                path="services/svc",
                boundary=["api/*.yaml"],
                behavior=["api/a.yaml"],
            )
            scene.file("services/svc/api/a.yaml", "openapi: 3.1.0\n")
            scene.commit()
            scene.file("services/svc/api/untracked.yaml", "openapi: 3.1.1\n")
            for source in ("head", "index", "working-tree"):
                with self.subTest(source=source):
                    self.assertEqual(_coverage_errors(scene, source), [])
                    self.assertEqual(
                        sorted(
                            _expand_component_paths(
                                scene.root, "services/svc", ["api/*.yaml"], source=source
                            )
                        ),
                        ["api/a.yaml"],
                    )
            # The premise: staging the same file does reach every view, so
            # the absences above are about tracking rather than about a
            # selector that matches nothing.
            scene.stage("services/svc/api/untracked.yaml")
            self.assertEqual(
                sorted(
                    _expand_component_paths(
                        scene.root, "services/svc", ["api/*.yaml"], source="index"
                    )
                ),
                ["api/a.yaml", "api/untracked.yaml"],
            )


# ---------------------------------------------------------------------------
# OBL-GLOBS-014 - the hand-written SemVer grammar's boundaries
# ---------------------------------------------------------------------------

#: Versions the grammar accepts, with the triple `parse_semver` must return.
#: The two-component and leading-`v` rows are the deliberate extensions; the
#: rest are the shapes the rejection table below differs from by one character.
SEMVER_ACCEPTED: Dict[str, Tuple[str, str, str]] = {
    "1.2.3": ("1", "1.2", "1.2.3"),
    "0.0.4": ("0", "0.0", "0.0.4"),
    "1.2": ("1", "1.2", "1.2.0"),
    "0.2": ("0", "0.2", "0.2.0"),
    "v1.2.3": ("1", "1.2", "1.2.3"),
    "v1.2": ("1", "1.2", "1.2.0"),
    "1.2-alpha": ("1", "1.2", "1.2.0"),
    "1.2.3-alpha.1": ("1", "1.2", "1.2.3"),
    "1.2.3-0a": ("1", "1.2", "1.2.3"),
    "1.0.0-0.3.7": ("1", "1.0", "1.0.0"),
    "1.2.3-alpha+001": ("1", "1.2", "1.2.3"),
    "1.2.3+build.01": ("1", "1.2", "1.2.3"),
}

#: Versions the grammar rejects, grouped by the rule that rejects them. Every
#: one must come back as (None, None, the input verbatim) - the third slot is
#: what a caller prints, so a grammar that normalized on the way out would be
#: reporting a version the user never wrote.
SEMVER_REJECTED: Dict[str, Tuple[str, ...]] = {
    "leading zero in a core identifier": ("01.2.3", "1.02.3", "1.2.03", "v01.2.3", "01.2"),
    "leading zero in a numeric prerelease identifier": ("1.2.3-01", "1.2.3-a.01"),
    "an empty prerelease or build segment": ("1.2.3-", "1.2.3+", "1.2.3-a..b", "1.2.3+a..b", "1.2.3-+b"),
    "a second plus in build metadata": ("1.2.3+a+b", "1.2.3+a+", "1.2.3-a+b+c"),
    "an uppercase V": ("V1.2.3", "V1.2", "V0.0.4"),
    "a trailing character": ("1.2.3x", "1.2.3 ", "1.2.3.4", "1.2.3-", "1.2."),
}


class SemverGrammarBoundaryTests(unittest.TestCase):
    """OBL-GLOBS-014: the extensions it makes and the rules it keeps."""

    def test_the_documented_extensions_and_ordinary_shapes_parse(self):
        """The premise under every rejection below.

        Each rejected input differs from an accepted one by a single
        character, so this table is what makes "(None, None, verbatim)"
        evidence about the rule rather than about the parser refusing
        everything.
        """
        for version, expected in SEMVER_ACCEPTED.items():
            with self.subTest(version=version):
                self.assertEqual(parse_semver(version), expected)

    def test_each_documented_rejection_returns_the_input_verbatim(self):
        for rule_name, versions in SEMVER_REJECTED.items():
            for version in versions:
                with self.subTest(rule=rule_name, version=version):
                    self.assertEqual(parse_semver(version), (None, None, version))

    def test_a_leading_zero_is_refused_in_the_core_and_allowed_in_build_metadata(self):
        """The one place a leading zero is legal, so the guard is not blanket.

        `_consume_semver_identifiers` is called twice with different
        `reject_numeric_leading_zeroes`, and a change that passed the same
        value both times would still satisfy the rejection table above.
        """
        self.assertEqual(parse_semver("1.2.3+build.01"), ("1", "1.2", "1.2.3"))
        self.assertEqual(parse_semver("1.2.3+01"), ("1", "1.2", "1.2.3"))
        self.assertEqual(parse_semver("1.2.3-01"), (None, None, "1.2.3-01"))

    def test_an_absent_version_carries_no_verbatim_third_slot(self):
        """The one rejection shape that does not echo its input."""
        for version in (None, "", 0, 1.2, ["1.2.3"]):
            with self.subTest(version=version):
                self.assertEqual(parse_semver(version), (None, None, None))


# ---------------------------------------------------------------------------
# OBL-GLOBS-015 - scoped --update against a global preflight failure
# ---------------------------------------------------------------------------

SCOPED_DIAGNOSTIC = (
    "LOCKFILE scoped --update cannot repair global component or slice preflight "
    "issues; rerun without --components after reviewing the full lock"
)
REPAIR_LINE = "after successful preflight repair."
LOCK_NAME = "boundary.lock.json"


def _locked_repository() -> Scenario:
    """A two-component, one-slice repository whose lock is committed and clean."""
    scene = Scenario(project="scenario")
    scene.component(
        "svc", path="services/svc", boundary=["api/a.yaml"], behavior=["api"]
    )
    scene.component("cli", path="apps/cli", boundary=["main.py"], behavior=["main.py"])
    scene.file("services/svc/api/a.yaml", "openapi: 3.1.0\n")
    scene.file("apps/cli/main.py", "print('x')\n")
    scene.slice("public", components=["svc"])
    scene.commit()
    (scene.root / LOCK_NAME).write_text(
        json.dumps(scene.generate(source="head"), indent=2) + "\n", encoding="utf-8"
    )
    scene.commit("lock")
    return scene


def _add_a_component(scene: Scenario) -> None:
    scene.component(
        "extra", path="apps/extra", boundary=["main.py"], behavior=["main.py"]
    )
    scene.file("apps/extra/main.py", "print('e')\n")
    scene.commit("declare another component")


def _add_a_slice(scene: Scenario) -> None:
    scene.slice("internal", components=["cli"])
    scene.commit("declare another slice")


def _rename_the_project(scene: Scenario) -> None:
    scene.config["project"] = "renamed"
    scene.commit("rename the project")


def _plant_a_digest_error(scene: Scenario) -> None:
    path = scene.root / LOCK_NAME
    lockfile = json.loads(path.read_text(encoding="utf-8"))
    lockfile["components"]["svc"]["exact_errors"] = ["synthetic exact failure"]
    path.write_text(json.dumps(lockfile, indent=2) + "\n", encoding="utf-8")
    scene.commit("plant a recorded digest error")


#: The four global preflight failures `_verify_lock_preflight_issues` can
#: report, each paired with the diagnostic line it produces. The existing
#: suite drives one of them into each half of the obligation; these run every
#: one through both halves of the same fixture.
PREFLIGHT_TRIGGERS: Dict[str, Tuple[Callable[[Scenario], None], str]] = {
    "component set": (
        _add_a_component,
        "LOCKFILE component set differs from config: "
        "locked=['cli', 'svc'] configured=['cli', 'extra', 'svc']",
    ),
    "slice set": (
        _add_a_slice,
        "LOCKFILE slice set differs from config: "
        "locked=['public'] configured=['internal', 'public']",
    ),
    "metadata mismatch": (
        _rename_the_project,
        "METADATA MISMATCH project: lockfile='scenario' current='renamed'",
    ),
    "locked digest error": (
        _plant_a_digest_error,
        "LOCKED DIGEST ERROR svc: synthetic exact failure",
    ),
}


class ScopedPreflightUpdateTests(unittest.TestCase):
    """OBL-GLOBS-015: what --components is allowed to repair, and what it is not."""

    def _lock_text(self, scene: Scenario) -> str:
        return (scene.root / LOCK_NAME).read_text(encoding="utf-8")

    def test_the_fixture_verifies_clean_before_any_trigger_is_applied(self):
        """The premise under every preflight failure below.

        Each trigger asserts a specific diagnostic; that only means
        something if the untouched repository produces none.
        """
        with _locked_repository() as scene:
            result = run_cli(scene.root, "verify", "--source", "head")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("preflight", result.stderr)

    def test_a_scoped_update_refuses_every_global_preflight_failure(self):
        for label, (trigger, diagnostic) in PREFLIGHT_TRIGGERS.items():
            with self.subTest(trigger=label), _locked_repository() as scene:
                trigger(scene)
                before = self._lock_text(scene)
                result = run_cli(
                    scene.root,
                    "verify",
                    "--source",
                    "head",
                    "--components",
                    "svc",
                    "--update",
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("ERROR: lockfile preflight failed:", result.stderr)
                self.assertIn(diagnostic, result.stderr)
                self.assertIn(SCOPED_DIAGNOSTIC, result.stderr)
                self.assertNotIn(REPAIR_LINE, result.stdout)
                self.assertEqual(self._lock_text(scene), before, label)

    def test_changed_from_with_components_is_not_a_scoped_update(self):
        """Naming both --components and --changed-from is a full regeneration.

        --changed-from empties the component filter on purpose and revalidates
        the whole lock, so a run that also names --components is not scoped and
        must not be refused as though it were. `scoped_preflight_update` says
        so with its last conjunct, `and not args.changed_from`, and deleting
        that conjunct was invisible to the entire suite (MUT-GLOBS-306): the
        scoped row passes no --changed-from, the --changed-from row elsewhere
        passes no --components, and the one test that passes both carries only
        drift rather than a preflight failure, so the branch is never entered.

        The base commit is taken before the trigger, because every trigger
        commits and the diff needs a ref from before it.
        """
        for label, (trigger, _diagnostic) in PREFLIGHT_TRIGGERS.items():
            with self.subTest(trigger=label), _locked_repository() as scene:
                base = scene.head()
                trigger(scene)
                before = self._lock_text(scene)
                result = run_cli(
                    scene.root,
                    "verify",
                    "--source",
                    "head",
                    "--components",
                    "svc",
                    "--changed-from",
                    base,
                    "--update",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn(SCOPED_DIAGNOSTIC, result.stderr + result.stdout)
                self.assertNotEqual(
                    self._lock_text(scene),
                    before,
                    f"{label}: a full regeneration must rewrite the lock",
                )

    def test_the_same_run_without_changed_from_is_still_refused(self):
        """The contrast: --components alone still gets the scoped refusal.

        Without this, the acceptance above would be satisfied by a build that
        had simply stopped refusing scoped updates altogether, which is the
        opposite of what the conjunct protects.
        """
        for label, (trigger, _diagnostic) in PREFLIGHT_TRIGGERS.items():
            with self.subTest(trigger=label), _locked_repository() as scene:
                trigger(scene)
                result = run_cli(
                    scene.root,
                    "verify",
                    "--source",
                    "head",
                    "--components",
                    "svc",
                    "--update",
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(SCOPED_DIAGNOSTIC, result.stderr)

    def test_an_unscoped_update_repairs_every_global_preflight_failure(self):
        """The other half of the pair, and the premise for "left unchanged".

        The same fixture and the same trigger, minus `--components`, must
        rewrite the lock and exit zero. Without this the refusals above
        would be consistent with a writer that never works.
        """
        for label, (trigger, _) in PREFLIGHT_TRIGGERS.items():
            with self.subTest(trigger=label), _locked_repository() as scene:
                trigger(scene)
                before = self._lock_text(scene)
                result = run_cli(
                    scene.root, "verify", "--source", "head", "--update"
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(REPAIR_LINE, result.stdout)
                self.assertIn(LOCK_NAME, result.stdout)
                self.assertNotIn(SCOPED_DIAGNOSTIC, result.stdout + result.stderr)
                self.assertNotEqual(self._lock_text(scene), before, label)

    def test_the_appended_diagnostic_is_what_raises_the_exit_code_to_two(self):
        """A project rename alone exits 1; scoping the repair makes it 2.

        `METADATA MISMATCH project` carries none of the safety prefixes
        `_drift_exit_code` looks for, so on its own it reads as ordinary
        drift. The appended line begins with `LOCKFILE`, and that is the
        whole difference between the second and third row here.
        """
        with _locked_repository() as scene:
            _rename_the_project(scene)
            rows = {
                "no scope, no update": (["verify", "--source", "head"], 1, False),
                "scoped, no update": (
                    ["verify", "--source", "head", "--components", "svc"],
                    1,
                    False,
                ),
                "scoped update": (
                    ["verify", "--source", "head", "--components", "svc", "--update"],
                    2,
                    True,
                ),
            }
            for label, (arguments, code, appended) in rows.items():
                with self.subTest(invocation=label):
                    result = run_cli(scene.root, *arguments)
                    self.assertEqual(result.returncode, code, result.stderr)
                    self.assertIn(
                        "METADATA MISMATCH project: lockfile='scenario' current='renamed'",
                        result.stderr,
                    )
                    self.assertIs(SCOPED_DIAGNOSTIC in result.stderr, appended)

    def test_the_refusal_reaches_machine_output_as_a_second_issue(self):
        """JSON callers see both lines and the same exit code."""
        with _locked_repository() as scene:
            _rename_the_project(scene)
            result = run_cli(
                scene.root,
                "verify",
                "--source",
                "head",
                "--components",
                "svc",
                "--update",
                "--format",
                "json",
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"])
            self.assertFalse(payload["updated"])
            self.assertEqual(
                payload["issues"],
                [
                    "METADATA MISMATCH project: lockfile='scenario' current='renamed'",
                    SCOPED_DIAGNOSTIC,
                ],
            )


# ---------------------------------------------------------------------------
# OBL-GLOBS-018 - only a whole ** segment is recursive
# ---------------------------------------------------------------------------

#: One pattern per shape the obligation names, with the compiled part kinds
#: `_compile_path_glob_with_spender` must produce. `recursive` appears only
#: where a segment is exactly `**`; everywhere else an in-segment `**` is an
#: ordinary single-segment wildcard.
COMPILED_PART_KINDS: Dict[str, Tuple[str, ...]] = {
    "src/**.py": ("literal", "glob"),
    "**.yaml": ("glob",),
    "x**": ("glob",),
    "src/**foo/bar": ("literal", "glob", "literal"),
    "a**/b": ("glob", "literal"),
    "**": ("recursive",),
    "src/**": ("literal", "recursive"),
    "src/**/*.py": ("literal", "recursive", "glob"),
    "**/**/a.py": ("recursive", "literal"),
}

#: What each shape must and must not select. The nested candidate is the one
#: that separates a single-segment wildcard from a recursive segment.
SEGMENT_REACH: Dict[str, Tuple[Tuple[str, bool], ...]] = {
    "src/**.py": (("src/a.py", True), ("src/.py", True), ("src/a/b.py", False)),
    "**.yaml": ((".yaml", True), ("a.yaml", True), ("a/b.yaml", False)),
    "x**": (("x", True), ("xyz", True), ("x/y", False)),
    "src/**foo/bar": (
        ("src/foo/bar", True),
        ("src/xfoo/bar", True),
        ("src/a/foo/bar", False),
    ),
    "a**/b": (("a/b", True), ("aq/b", True), ("a/q/b", False)),
    "src/**/*.py": (("src/a.py", True), ("src/a/b.py", True), ("src/a/b/c.py", True)),
    "src/**": (("src", True), ("src/a", True), ("src/a/b", True)),
}


class InSegmentRecursiveWildcardTests(unittest.TestCase):
    """OBL-GLOBS-018: `src/**.py` is a wildcard, not a recursion."""

    def test_only_an_exact_double_star_segment_compiles_to_a_recursive_part(self):
        """The compiler's own branch, read off the compiled program.

        `_compile_path_glob_with_spender` decides this on `token == "**"`,
        and nothing in the suite pins which side of that branch a segment
        that merely contains `**` lands on.
        """
        for pattern, kinds in COMPILED_PART_KINDS.items():
            with self.subTest(pattern=pattern):
                compiled = _compile_path_glob(pattern)
                self.assertIsNotNone(compiled, pattern)
                self.assertEqual(tuple(part[0] for part in compiled.parts), kinds)

    def test_a_segment_that_merely_contains_double_star_never_crosses_a_separator(self):
        """The matcher, on candidates chosen so crossing changes the answer.

        Each row carries a positive as well as the negative, so a pattern
        that matched nothing at all could not pass.
        """
        for pattern, rows in SEGMENT_REACH.items():
            for candidate, expected in rows:
                with self.subTest(pattern=pattern, candidate=candidate):
                    self.assertIs(_match_path_glob(candidate, pattern), expected)

    def test_a_declaration_written_that_way_omits_every_nested_file_and_still_generates(self):
        """The consequence a user meets, all the way through to the digest.

        `api/**.yaml` reads like recursion and behaves like `api/*.yaml`.
        Validation raises nothing, generation succeeds, and the boundary
        digest is the one for the root-level file alone - which is the
        silent omission the obligation is about.
        """

        def build(boundary: List[str]) -> Scenario:
            scene = Scenario()
            scene.component(
                "svc", path="services/svc", boundary=boundary, behavior=["api"]
            )
            scene.file("services/svc/api/a.yaml", "openapi: 3.1.0\n")
            scene.file("services/svc/api/v1/b.yaml", "openapi: 3.1.1\n")
            scene.commit()
            return scene

        with build(["api/a.yaml"]) as scene:
            root_only = scene.digest("svc", "boundary")
        with build(["api/a.yaml", "api/v1/b.yaml"]) as scene:
            everything = scene.digest("svc", "boundary")
        self.assertNotEqual(root_only, everything)

        with build(["api/**.yaml"]) as scene:
            self.assertEqual(
                sorted(
                    _expand_component_paths(
                        scene.root, "services/svc", ["api/**.yaml"], source="head"
                    )
                ),
                ["api/a.yaml"],
            )
            self.assertEqual(_coverage_errors(scene, "head"), [])
            self.assertEqual(scene.digest("svc", "boundary"), root_only)

        # The premise: the same declaration with a complete `**` segment does
        # reach the nested file, so the omission above is about the grammar
        # rather than about a repository the nested file never entered.
        with build(["api/**/*.yaml"]) as scene:
            self.assertEqual(
                sorted(
                    _expand_component_paths(
                        scene.root, "services/svc", ["api/**/*.yaml"], source="head"
                    )
                ),
                ["api/a.yaml", "api/v1/b.yaml"],
            )
            self.assertEqual(scene.digest("svc", "boundary"), everything)


# ---------------------------------------------------------------------------
# OBL-GLOBS-021 - the aggregate budget on one operation
# ---------------------------------------------------------------------------


def _reference_segment_match(candidate: str, pattern: str) -> bool:
    """Split on `/`, let fnmatch decide one segment, let `**` span segments.

    The per-segment verdict is CPython's and the recursion is a plain NFA
    written here, so this shares no code with the matcher under test. It is
    only valid for patterns whose segments contain no bracket expression,
    which is why the strategies below draw from a named token list.
    """
    path_parts = candidate.split("/")
    pattern_parts = pattern.split("/")

    def closure(states: Set[int]) -> Set[int]:
        closed = set(states)
        pending = list(states)
        while pending:
            index = pending.pop()
            if (
                index < len(pattern_parts)
                and pattern_parts[index] == "**"
                and index + 1 not in closed
            ):
                closed.add(index + 1)
                pending.append(index + 1)
        return closed

    states = closure({0})
    for segment in path_parts:
        following: Set[int] = set()
        for index in states:
            if index >= len(pattern_parts):
                continue
            token = pattern_parts[index]
            if token == "**":
                following.add(index)
            elif fnmatch.fnmatchcase(segment, token):
                following.add(index + 1)
        states = closure(following)
        if not states:
            return False
    return len(pattern_parts) in states


#: Pattern segments the operation properties draw from. Each names a shape the
#: segment grammar treats differently, and none contains a bracket expression,
#: because the oracle above delegates a segment to fnmatch.
OPERATION_SEGMENTS = ("a", "ab", "a.py", "*", "*.py", "?", "**", "**.py", "a**", "x*y")

#: Candidate paths are drawn from an alphabet that overlaps the literals above
#: on purpose: with disjoint characters almost every draw would be a rejection
#: and the stability claim would be about False alone.
OPERATION_ALPHABET = "abcxy."


def operation_patterns() -> st.SearchStrategy:
    return st.lists(
        st.sampled_from(OPERATION_SEGMENTS), min_size=1, max_size=4
    ).map("/".join)


def operation_paths() -> st.SearchStrategy:
    return st.lists(
        st.text(alphabet=OPERATION_ALPHABET, min_size=1, max_size=4),
        min_size=1,
        max_size=4,
    ).map("/".join)


BUDGET_PROFILE = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


class PathGlobOperationMachine(RuleBasedStateMachine):
    """One operation object, driven through an arbitrary interleaving.

    The three claims checked on every step are that the answer to a pair
    never changes within an operation, that `steps` never falls, and that a
    cache hit in `matches` still charges its match work.
    """

    patterns = Bundle("patterns")

    def __init__(self) -> None:
        super().__init__()
        self.operation = _PathGlobOperation("Component path expansion")
        self.answers: Dict[Tuple[str, str], bool] = {}
        self.prepared: Set[str] = set()
        self.last_steps = 0

    @rule(target=patterns, pattern=operation_patterns())
    def declare(self, pattern: str) -> str:
        return pattern

    @rule(pattern=patterns)
    def prepare_only(self, pattern: str) -> None:
        before = self.operation.steps
        compiled = self.operation.prepare(pattern)
        assert compiled.pattern == pattern
        if pattern in self.prepared:
            assert self.operation.steps == before, "a cached compile charged again"
        else:
            assert self.operation.steps > before, "a fresh compile charged nothing"
        self.prepared.add(pattern)
        assert self.operation.compiled_patterns == len(self.prepared)

    @rule(pattern=patterns, path=operation_paths())
    def evaluate(self, pattern: str, path: str) -> None:
        before = self.operation.steps
        answer = self.operation.matches(path, pattern)
        assert self.operation.steps > before, "a match charged nothing"
        assert answer is _reference_segment_match(path, pattern), (
            f"{pattern!r} against {path!r}"
        )
        remembered = self.answers.setdefault((path, pattern), answer)
        assert answer is remembered, f"{pattern!r} against {path!r} changed answer"
        self.prepared.add(pattern)
        assert self.operation.compiled_patterns == len(self.prepared)

    @invariant()
    def steps_never_fall(self) -> None:
        assert self.operation.steps >= self.last_steps
        self.last_steps = self.operation.steps


class PathGlobOperationBudgetTests(unittest.TestCase):
    """OBL-GLOBS-021: exhaustion raises, and everything else stays put."""

    #: One pair per interesting shape, with the answer stated so a reader can
    #: see that both verdicts are represented. The budget sweep needs both,
    #: because "never degrades to False" is only half the claim: an exhausted
    #: budget must not answer False even when False is the truth.
    SWEEP_PAIRS = {
        ("src/a/b.py", "src/**/*.py"): True,
        ("src/a.py", "src/**.py"): True,
        ("src/a/b.py", "src/**.py"): False,
        ("a/b/c/d", "**"): True,
        ("api/v1.yaml", "api/v?.yaml"): True,
        ("api/v1.yaml", "api/*.json"): False,
    }

    def test_one_operation_answers_a_pair_the_same_way_however_it_is_reached(self):
        """The state machine, over arbitrary interleavings of prepare and match."""
        run_state_machine_as_test(
            PathGlobOperationMachine,
            settings=settings(
                max_examples=60,
                stateful_step_count=40,
                deadline=None,
                suppress_health_check=[HealthCheck.too_slow],
            ),
        )

    def test_the_ten_thousandth_evaluation_answers_what_the_first_did(self):
        """The obligation's own number, against which a hundred is short.

        Six pairs over five patterns are interleaved so the compile cache is
        exercised rather than one entry read ten thousand times, and the
        aggregate budget is asserted to have been spent rather than merely
        available.
        """
        operation = _PathGlobOperation("Component path expansion")
        pairs = list(self.SWEEP_PAIRS)
        first = {pair: operation.matches(pair[0], pair[1]) for pair in pairs}
        self.assertEqual(operation.compiled_patterns, len({p[1] for p in pairs}))
        steps = operation.steps
        for round_number in range(2500):
            for pair in pairs:
                answer = operation.matches(pair[0], pair[1])
                if answer is not first[pair]:  # pragma: no cover - the failure
                    self.fail(
                        f"{pair} answered {answer} at round {round_number}, "
                        f"not {first[pair]}"
                    )
                self.assertGreaterEqual(operation.steps, steps)
                steps = operation.steps
        self.assertEqual(first, dict(self.SWEEP_PAIRS))
        self.assertEqual(operation.compiled_patterns, len({p[1] for p in pairs}))
        self.assertGreater(operation.steps, 100_000)

    def test_a_budget_below_the_measured_cost_raises_rather_than_answering(self):
        """The sweep, with the threshold measured instead of guessed.

        A generous operation is run once and its `steps` counter read; that
        number is the cost of the evaluation. Every budget below it must
        raise - which is what makes the property below non-vacuous - and
        every budget from it upward must return the true answer, including
        where the true answer is False.
        """
        for (path, pattern), truth in self.SWEEP_PAIRS.items():
            with self.subTest(pattern=pattern, path=path):
                generous = _PathGlobOperation("Component path expansion")
                self.assertIs(generous.matches(path, pattern), truth)
                cost = generous.steps
                self.assertGreater(cost, 0)
                for budget in range(cost + 4):
                    operation = _PathGlobOperation(
                        "Component path expansion", max_steps=budget
                    )
                    if budget < cost:
                        with self.assertRaises(GuardrailError):
                            operation.matches(path, pattern)
                    else:
                        self.assertIs(operation.matches(path, pattern), truth, budget)

    @BUDGET_PROFILE
    @given(
        pattern=operation_patterns(),
        path=operation_paths(),
        budget=st.integers(min_value=0, max_value=400),
    )
    def test_a_tight_budget_never_turns_a_verdict_into_the_other_one(
        self, pattern: str, path: str, budget: int
    ):
        """Exhaustion may raise; it may not answer.

        The oracle is the segment model in this file rather than a second
        run of the matcher, so a budget that silently degraded both the
        answer and the reference would still be caught.
        """
        operation = _PathGlobOperation("Component path expansion", max_steps=budget)
        try:
            answer = operation.matches(path, pattern)
        except GuardrailError:
            return
        self.assertIs(
            answer,
            _reference_segment_match(path, pattern),
            f"budget {budget} on {pattern!r} against {path!r}",
        )

    def test_a_compile_that_ran_out_of_budget_leaves_the_cache_empty(self):
        """`prepare` assigns only after a successful compile, twice over.

        The premise is the generous operation beside it: a compile that
        succeeds does add an entry, so `compiled_patterns == 0` below is
        about the failure rather than about a counter that never moves.
        """
        pattern = "a/b/c/d/e"
        generous = _PathGlobOperation("Component path expansion")
        generous.prepare(pattern)
        self.assertEqual(generous.compiled_patterns, 1)

        starved = _PathGlobOperation("Component path expansion", max_steps=2)
        with self.assertRaises(GuardrailError) as first:
            starved.prepare(pattern)
        self.assertIn(
            "Component path expansion guardrail exceeded: more than 2 "
            "aggregate glob compile/match steps",
            str(first.exception),
        )
        self.assertEqual(starved.compiled_patterns, 0)
        spent = starved.steps
        with self.assertRaises(GuardrailError):
            starved.prepare(pattern)
        self.assertEqual(starved.compiled_patterns, 0)
        self.assertEqual(starved.steps, spent)

    def test_a_pattern_that_cannot_compile_fails_closed_and_caches_nothing(self):
        """An empty segment is refused as an error, never as a non-match."""
        operation = _PathGlobOperation("Component path expansion")
        with self.assertRaises(GuardrailError) as refused:
            operation.matches("a/b", "a//b")
        self.assertEqual(
            str(refused.exception),
            "Component path expansion failed closed: invalid path glob 'a//b'",
        )
        self.assertEqual(operation.compiled_patterns, 0)

    def test_a_cache_hit_charges_its_match_steps_and_not_its_compile_steps(self):
        """Both halves, because either alone is satisfiable by a bug.

        "The second call charged something" would pass if the pattern were
        recompiled every time; "the second call charged less" would pass if
        it charged nothing at all.
        """
        operation = _PathGlobOperation("Component path expansion")
        self.assertIs(operation.matches("src/a/b.py", "src/**/*.py"), True)
        first = operation.steps
        self.assertIs(operation.matches("src/a/b.py", "src/**/*.py"), True)
        second = operation.steps - first
        self.assertGreater(second, 0)
        self.assertLess(second, first)
        self.assertEqual(operation.compiled_patterns, 1)

    def test_a_negative_work_limit_is_refused_at_construction(self):
        with self.assertRaises(ValueError):
            _PathGlobOperation("Component path expansion", max_steps=-1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
