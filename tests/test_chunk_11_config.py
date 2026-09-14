"""Six promises about input boundver does not author: a baseline, a contract, an API call.

A verification baseline is committed JSON that tells CI which failures it may
ignore, so the only thing between a crafted entry and a suppressed integrity
failure is `violation_identity` returning None for the diagnostics that must
never be acknowledged. Picking one string and checking it proves very little,
because the attack is not one string but the whole space of identities a
schema-valid entry can spell. This file enumerates that space instead:
`_KIND_FACETS` supplies every kind and every facet the baseline format permits,
the subjects are read back out of the diagnostics themselves, and the resulting
entries are put in front of `apply_baseline` for every non-baselinable family.
The families are not listed by hand either - the prefixes are lifted from
`core._drift_exit_code`'s own `safety_prefixes` tuple with `ast`, so a prefix
added there without a row here fails the enumeration instead of going untested.
Enumerating that space is not the same as attacking the regexes that define it.
None of the crafted tails in the table can be reached by an identity regex that
stopped anchoring at the start of the string, because every one of them lacks
the literal `MISMATCH ` those patterns require, so the tails are chosen for
realism and prove nothing about anchoring. The strings that would be reached
are collected in a table of their own, and one of them is not crafted at all: a
component whose declared boundary path is the text
`SLICE MISMATCH all.boundary:` passes both validators and produces an ordinary
`CURRENT DIGEST ERROR` quoting it, so an unanchored `_SLICE_FACET_RE` would read
a baselinable slice identity straight out of an integrity failure. Both halves
are asserted for every trap: that it really does match the unanchored pattern,
and that the product refuses it anyway even when a baseline holds exactly the
identity the unanchored pattern would have produced.
Four of those families are then produced for real by a repository and the
diagnostics are harvested from a live `verify --format json`, which is what
stops the crafted strings from drifting away from the product's wording; the
whole thing is closed by an end-to-end run where a legitimately written
baseline is hand-edited with six extra entries and the broken repository still
exits 2 with every integrity failure in `issues`. Two results came out of that
work. `VENDORED DRIFT` is produced from the bounded vendored-error detail for a
readable copy whose content differs, while missing or unreadable copy inputs
remain `CURRENT DIGEST ERROR` safety failures. The obligation's "never remove
a metadata mismatch" is one case too strong: a
`METADATA MISMATCH <c>.version` or `.semver` line *is* acknowledged, but only
when a compat mismatch for the same component is both present in this run and
baselined, which is the "never independently acknowledged" rule the spec
actually states. Pinning that rule takes an acknowledged line and a refused one
in the same `apply_baseline` call, because the acknowledgement is keyed on the
component and the field is the only thing keeping it narrow: beside a baselined
and currently mismatching compat failure, `METADATA MISMATCH svc.version` is
acknowledged while `METADATA MISMATCH svc.boundary_status` must not be, and only
a case holding the two together can tell a field-specific rule from a
component-wide one. Both findings are pinned rather than softened.

The canonical OpenAPI provider is the other place a repository's own bytes
decide what a digest means, and there the interesting question is what PyYAML
would have done if the resolver replacement were removed. Asserting the loaded
type is only half an answer, so every YAML 1.1 trap is also checked at the
digest: quoting `no`, `on`, `012`, `12:30` or `2024-01-01` must leave the
`openapi-canonical` boundary digest bit for bit unchanged, because those were
already strings, while quoting `true` must move it - that last row is the
premise, and without it the whole table would pass against a provider that had
stopped hashing the document at all. The reference policy came out worse. Two
of the three edges the obligation names are divergences: a non-string `$ref`
(`3`, `null`, a list) is reported as an external reference rather than as
malformed, and a schema that legally declares a property named `$ref` is
rejected outright with an error telling the author to fix a reference that does
not exist. Both are marked `expectedFailure` with the current behaviour pinned
beside them. The obligation's third edge, the located path, is real but
misnamed: the product emits `$.components.schemas.Thing.$ref`, a `$`-rooted
path, not the RFC 6901 pointer the obligation asks for.

The embeddable API needed the least machinery and produced the sharpest
finding. `verify()` agrees with `verify --format json` element for element
across the whole corpus, generated invocations included, but
`dump_lockfile(generate(out_path=None))` is *not* byte-identical to what
`generate --format json` prints, because `_print_json` renders with
`sort_keys=True` and `dump_lockfile` does not; the two documents parse equal
and the API rendering is byte-identical to the file the CLI writes, so all
three relations are pinned separately. The no-write guarantee is checked by
hashing every file and directory under the repository root, which is only
meaningful because the same snapshot demonstrably notices the lockfile a real
`generate(out_path=...)` leaves behind, and the two silence checks are
meaningful only because the same `redirect_stdout` nesting demonstrably catches
the CLI's own printing.

The config parity property needed `sys.modules["jsonschema"] = None` to make
`_schema_engine_errors` take its `except ImportError` branch. Its premise has to
run through `validate_config` rather than through `_schema_engine_errors`,
because the two sides of the property are supersets of one another: if the
engine were ever disconnected from `validate_config` - which is the state a
zipapp is permanently in, and the state this obligation is about - the property
would degenerate into comparing the hand validator with itself and pass forever.
On an empty slice key the two calls differ by exactly the schema's own line, and
that difference is what the premise asserts. The property is one-directional
even so: a rule living only in the hand validator can never make it fail, so it
covers the zipapp half of the obligation and not the editor-clean-config half.
Two of the three suspects the obligation names turned out to be already covered
by the hand validator. The third, `maxItems` on a slice's member list, really is
schema-only - the hand validator has no length rule at all - but it is still not
a parity hole, because a config may declare at most `MAX_CONSUMER_GRAPH_ITEMS`
components and the schema caps a slice's member list at the same number, so any
list long enough to trip the cap has to repeat a name or name a component that
does not exist, and the hand validator rejects both.

Two fixture details cost more than they look. The repository the generated
invocations run against has to make every facet *available*, not merely
declarable: an explicit `verify_facets` naming `compat` without a
`version_source`, or `behavior` without `behavior.paths`, is a static config
error, so a two-file fixture would have spent half its draws failing validation
instead of comparing two verifiers. And the config mutations compose, which
means `no components` can be drawn alongside a row that writes into the
component it just deleted; those rows go through a helper that hands them a
scratch dict, so the pair collapses to the stronger mutation rather than
raising inside the corpus builder and reporting a test bug as a divergence.

Covers OBL-HASHING-085, OBL-PROVIDERS-009, OBL-PROVIDERS-011, OBL-PROVIDERS-059,
OBL-PROVIDERS-061 and OBL-CONFIG-010.
"""

from __future__ import annotations

import ast
import contextlib
import copy
import hashlib
import inspect
import io
import itertools
import json
import os
import re
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import boundver
from boundver import _utils, core
from boundver._baseline import (
    _COMPONENT_FACET_RE,
    _KIND_FACETS,
    _SLICE_FACET_RE,
    BaselineError,
    _identity_entry,
    apply_baseline,
    violation_identity,
)
from boundver._canonical_providers import _openapi_document_error, _parse_yaml_or_json
from boundver._config import (
    _load_config_schema,
    _schema_engine_errors,
    validate_config,
)
from boundver._config_contract import MAX_CONSUMER_GRAPH_ITEMS
from boundver._lockfile import COMPONENT_METADATA_FIELDS, dump_lockfile
from boundver._utils import (
    DIAGNOSTIC_TRUNCATION_SENTINEL,
    FACET_SET,
    BoundverError,
    ConfigError,
    LockfileError,
)

from tests._parity import run_cli, run_cli_in_process
from tests._scenarios import SOURCE_MODES, Scenario

PROFILE = settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.filter_too_much, HealthCheck.too_slow],
)

CONFIG_PROFILE = settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.filter_too_much, HealthCheck.too_slow],
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _inside(root: Path):
    """Run inside *root*, because the public API resolves the repo from cwd.

    `_load_validated_config_inputs` calls `git_root()` with no argument, so
    every `boundver.verify()` and `boundver.generate()` here has to be issued
    from the scenario directory. The working directory is a process global and
    eleven other test files share this interpreter, so it is restored in a
    `finally` and always before the scenario is torn down - Windows cannot
    delete a directory that is somebody's cwd.
    """
    previous = Path.cwd()
    os.chdir(root)
    try:
        yield
    finally:
        os.chdir(previous)


def _tree_snapshot(root: Path) -> Dict[str, str]:
    """Every path under *root* outside .git, with file contents hashed.

    A leftover temporary file from the atomic writer is a new key; a rewritten
    lockfile is a changed value; a deleted file is a missing key. Directories
    are recorded too, because `_ensure_lock_outside_components` must not create
    the parent of a destination it is about to reject.
    """
    state: Dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            continue
        key = relative.as_posix()
        if path.is_dir():
            state[key + "/"] = "<dir>"
        else:
            state[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return state


# ---------------------------------------------------------------------------
# OBL-PROVIDERS-009: YAML 1.2 core scalars, not PyYAML's YAML 1.1 defaults
# ---------------------------------------------------------------------------

#: The minimum OpenAPI 3.1 document that still parses as YAML rather than JSON,
#: with one free slot for the scalar under test. The suffix has to stay
#: non-`.json` or `_parse_yaml_or_json` takes its strict-JSON branch and the
#: YAML loader is never reached.
YAML_DOCUMENT = (
    "openapi: 3.1.0\n"
    "info:\n"
    "  title: t\n"
    "  version: '1.0.0'\n"
    "paths: {}\n"
    "x: SCALAR\n"
)

#: One row per YAML scalar spelling, mapping the source text to the value the
#: YAML 1.2 core schema requires. Everything in the first block is a YAML 1.1
#: trap: PyYAML's stock SafeLoader turns each of them into a bool, an octal
#: int, a sexagesimal int or a datetime.date, and each such change rewrites the
#: canonical bytes the digest is taken over. The `true`/`false` rows are the
#: control: those are the only implicit booleans YAML 1.2 keeps.
YAML_CORE_SCALARS = {
    "yes": "yes",
    "no": "no",
    "on": "on",
    "off": "off",
    "y": "y",
    "n": "n",
    "Yes": "Yes",
    "OFF": "OFF",
    "012": "012",
    "0x1f": "0x1f",
    "0o17": "0o17",
    "12:30": "12:30",
    "2024-01-01": "2024-01-01",
    "2024-01-01T00:00:00Z": "2024-01-01T00:00:00Z",
    "true": True,
    "false": False,
    "True": True,
    "FALSE": False,
    "0": 0,
    "10": 10,
    "1.5": 1.5,
    "1e3": 1000.0,
    "null": None,
    "~": None,
}

#: The non-finite spellings YAML 1.2 still recognizes. The dedicated resolver
#: resolves each as a float precisely so the JSON-tree guard can refuse it;
#: leaving them as strings would be a silent type change and emitting them as
#: numbers would break JSON round-tripping of the canonical bytes.
YAML_NON_FINITE = (".inf", "-.Inf", "+.INF", ".NaN", ".nan")

#: Observed for a scalar written at the document's top level under key `x`.
NON_FINITE_ERROR = "$.x contains a non-finite number"

#: The scalar spellings whose meaning must not depend on quoting, because the
#: YAML 1.2 core schema already reads them as strings. `true` is not
#: quote-invariant and is the premise: it proves the digest under test really
#: does move when the loaded type changes, so a provider that had stopped
#: hashing the document could not pass the rest of the table.
QUOTE_INVARIANT_SCALARS = ("yes", "no", "on", "off", "y", "n", "012", "12:30",
                           "2024-01-01")

#: The same document with a schema default, so the scalar reaches the canonical
#: bytes through a real `openapi-canonical` boundary rather than a loose key.
CANONICAL_DOCUMENT = (
    "openapi: 3.1.0\n"
    "info:\n"
    "  title: t\n"
    "  version: '1.0.0'\n"
    "paths: {}\n"
    "components:\n"
    "  schemas:\n"
    "    Thing:\n"
    "      type: object\n"
    "      default: SCALAR\n"
)


def _canonical_digest(scalar: str) -> Tuple[Optional[str], Optional[List[str]]]:
    """Generate a lock for a one-file openapi-canonical component."""
    with Scenario() as scene:
        scene.component(
            "svc", path="svc", provider="openapi-canonical", boundary=["api.yaml"]
        )
        scene.file("svc/api.yaml", CANONICAL_DOCUMENT.replace("SCALAR", scalar))
        scene.commit()
        component = scene.generate()["components"]["svc"]
        return component["fingerprints"].get("boundary"), component.get(
            "boundary_errors"
        )


class YamlCoreScalarTests(unittest.TestCase):
    """OBL-PROVIDERS-009: the loader follows YAML 1.2, not PyYAML's defaults."""

    def _load(self, scalar: str) -> Any:
        return _parse_yaml_or_json(
            YAML_DOCUMENT.replace("SCALAR", scalar).encode("utf-8"), "contract.yaml"
        )

    def test_every_yaml_1_1_trap_keeps_its_yaml_1_2_core_value(self):
        for source, expected in YAML_CORE_SCALARS.items():
            with self.subTest(scalar=source):
                value = self._load(source)["x"]
                self.assertEqual(value, expected)
                self.assertIs(type(value), type(expected))

    def test_the_yaml_1_1_family_survives_as_strings_rather_than_booleans(self):
        """The narrow claim, stated separately so a partial fix cannot hide.

        Only `on` was exercised before this file, so a resolver regression that
        reinstated the `y|n` or `yes|no` half of PyYAML's bool pattern would
        have passed. Each spelling is asserted to be a `str` in its own right.
        """
        for source in ("yes", "no", "on", "off", "y", "n", "Yes", "OFF"):
            with self.subTest(scalar=source):
                value = self._load(source)["x"]
                self.assertIs(type(value), str)
                self.assertEqual(value, source)

    def test_a_bare_timestamp_stays_a_string_and_never_becomes_a_date(self):
        """`tag:yaml.org,2002:timestamp` is stripped from the resolver list.

        If that entry came back, a `datetime.date` would reach the JSON-tree
        guard rather than the canonical serializer, so the failure would show
        up far from its cause. Asserting the type here names the cause.
        """
        for source in ("2024-01-01", "2024-01-01T00:00:00Z", "2024-01-01 00:00:00"):
            with self.subTest(scalar=source):
                value = self._load(source)["x"]
                self.assertIs(type(value), str)

    def test_every_non_finite_spelling_is_rejected_by_the_json_tree_guard(self):
        for source in YAML_NON_FINITE:
            with self.subTest(scalar=source):
                document = self._load(source)
                self.assertIs(type(document["x"]), float)
                self.assertEqual(_openapi_document_error(document), NON_FINITE_ERROR)

    def test_a_finite_number_is_not_rejected_by_that_same_guard(self):
        """Premise: the guard the previous test relies on lets numbers through.

        Without this, the rejection above would also pass against a guard that
        refused every float, which would say nothing about non-finite spellings.
        """
        for source in ("1.5", "1e3", "0", "10"):
            with self.subTest(scalar=source):
                self.assertIsNone(_openapi_document_error(self._load(source)))

    def test_quoting_a_yaml_1_1_trap_does_not_move_the_canonical_digest(self):
        for scalar in QUOTE_INVARIANT_SCALARS:
            with self.subTest(scalar=scalar):
                bare, bare_errors = _canonical_digest(scalar)
                quoted, quoted_errors = _canonical_digest(f"'{scalar}'")
                self.assertIsNone(bare_errors)
                self.assertIsNone(quoted_errors)
                self.assertEqual(bare, quoted)

    def test_quoting_a_real_boolean_does_move_the_canonical_digest(self):
        """Premise for the table above: this digest is sensitive to the type.

        `true` and `'true'` are a boolean and a string under YAML 1.2, so their
        canonical bytes differ. If this pair ever agreed, every quote-invariance
        row would be passing for the wrong reason.
        """
        for scalar in ("true", "false", "0"):
            with self.subTest(scalar=scalar):
                bare, _ = _canonical_digest(scalar)
                quoted, _ = _canonical_digest(f"'{scalar}'")
                self.assertNotEqual(bare, quoted)

    def test_a_non_finite_scalar_fails_generation_instead_of_producing_a_digest(self):
        with self.assertRaises(ConfigError) as caught:
            _canonical_digest(".inf")
        self.assertEqual(
            str(caught.exception),
            "Lockfile generation failed:\n"
            "svc: OpenAPI canonicalization failed for api.yaml: "
            "$.components.schemas.Thing.default contains a non-finite number",
        )

    def test_a_quoted_non_finite_spelling_is_an_ordinary_string(self):
        """The rejection is about the value, not about the four characters."""
        digest, errors = _canonical_digest("'.inf'")
        self.assertIsNone(errors)
        self.assertIsNotNone(digest)


# ---------------------------------------------------------------------------
# OBL-PROVIDERS-011: only same-document fragment references
# ---------------------------------------------------------------------------


def _openapi_with_schema(schema: Any) -> dict:
    return {
        "openapi": "3.1.0",
        "info": {"title": "t", "version": "1.0.0"},
        "paths": {},
        "components": {"schemas": {"Thing": schema}},
    }


#: The prose half of the rejection, identical for every offending node.
REFERENCE_ERROR_TAIL = (
    " uses an external or local-file reference; openapi-canonical accepts only "
    "same-document fragment references beginning with '#'"
)

#: Reference values that must be accepted, mapped to why they are legal.
ACCEPTED_REFERENCES = {
    "#/components/schemas/Other": "an ordinary same-document pointer",
    "#": "the whole document",
    "#/paths/~1ping/get": "an escaped pointer",
}

#: Reference values that must be rejected, mapped to the exact located path the
#: product emits for a schema at $.components.schemas.Thing.
REJECTED_REFERENCES = {
    "./other.yaml#/x": "$.components.schemas.Thing.$ref",
    "../sibling.yaml#/x": "$.components.schemas.Thing.$ref",
    "other.yaml": "$.components.schemas.Thing.$ref",
    "https://example/x": "$.components.schemas.Thing.$ref",
    "file:///etc/passwd": "$.components.schemas.Thing.$ref",
}


class ReferencePolicyTests(unittest.TestCase):
    """OBL-PROVIDERS-011: the reference walk, at the three edges that matter."""

    def test_a_same_document_fragment_reference_is_accepted(self):
        for reference, reason in ACCEPTED_REFERENCES.items():
            with self.subTest(reference=reference, reason=reason):
                document = _openapi_with_schema({"$ref": reference})
                self.assertIsNone(_openapi_document_error(document))

    def test_an_external_reference_is_rejected_and_its_node_is_located(self):
        for reference, location in REJECTED_REFERENCES.items():
            with self.subTest(reference=reference):
                document = _openapi_with_schema({"$ref": reference})
                self.assertEqual(
                    _openapi_document_error(document), location + REFERENCE_ERROR_TAIL
                )

    def test_the_located_path_names_the_offending_node_and_not_its_parent(self):
        """The path is derived, so it can be wrong without the prose changing.

        A reference buried in an array item is the case that separates a real
        `_json_path_child` walk from a constant: the index has to appear.
        """
        document = {
            "openapi": "3.1.0",
            "info": {"title": "t", "version": "1.0.0"},
            "paths": {
                "/p": {
                    "get": {
                        "parameters": [
                            {"name": "a", "in": "query"},
                            {"$ref": "./other.yaml#/params/b"},
                        ],
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        }
        self.assertEqual(
            _openapi_document_error(document),
            "$.paths./p.get.parameters[1].$ref" + REFERENCE_ERROR_TAIL,
        )

    def test_a_root_level_reference_is_located_at_the_document_root(self):
        document = {
            "openapi": "3.1.0",
            "info": {"title": "t", "version": "1.0.0"},
            "paths": {},
            "$ref": "external.yaml",
        }
        self.assertEqual(
            _openapi_document_error(document), "$.$ref" + REFERENCE_ERROR_TAIL
        )

    def test_the_location_is_a_dollar_rooted_path_not_an_rfc_6901_pointer(self):
        """The obligation calls this an RFC 6901 path; it is not one.

        RFC 6901 would spell the same node `/components/schemas/Thing/$ref`.
        What the product emits is a `$`-rooted, dot-separated rendering. The
        substance the obligation wants - the offending node is named - holds;
        the notation it names does not. Both directions are pinned so a change
        to either spelling is visible.
        """
        error = _openapi_document_error(
            _openapi_with_schema({"$ref": "./other.yaml#/x"})
        )
        self.assertTrue(error.startswith("$.components.schemas.Thing.$ref"))
        self.assertNotIn("/components/schemas/Thing/$ref", error)

    def test_a_non_string_reference_value_is_rejected_as_malformed(self):
        for value in (3, None, ["#/x"], {"a": 1}, True):
            with self.subTest(value=value):
                error = _openapi_document_error(_openapi_with_schema({"$ref": value}))
                self.assertIsNotNone(error)
                self.assertNotIn("external or local-file reference", error)
                self.assertIn("is malformed", error)

    def test_a_non_string_reference_names_the_offending_node(self):
        for value in (3, None, ["#/x"], {"a": 1}, True):
            with self.subTest(value=value):
                self.assertEqual(
                    _openapi_document_error(_openapi_with_schema({"$ref": value})),
                    "$.components.schemas.Thing.$ref is malformed; expected a "
                    "string same-document fragment reference beginning with '#'",
                )

    def test_a_property_literally_named_ref_is_not_read_as_a_reference(self):
        document = _openapi_with_schema(
            {"type": "object", "properties": {"$ref": {"type": "string"}}}
        )
        self.assertIsNone(_openapi_document_error(document))

    def test_a_ref_spelling_in_contract_data_is_not_read_as_a_reference(self):
        for schema in (
            {"enum": [{"$ref": "https://example/data"}]},
            {"default": {"$ref": 3}},
            {"x-policy": {"$ref": "https://example/extension"}},
        ):
            with self.subTest(schema=schema):
                self.assertIsNone(_openapi_document_error(_openapi_with_schema(schema)))

    def test_data_like_named_map_entries_do_not_hide_external_references(self):
        reference = "other.yaml#/Thing"
        for name in ("const", "default", "enum", "x-policy"):
            documents = (
                (
                    {
                        "openapi": "3.1.0",
                        "paths": {},
                        "components": {"schemas": {name: {"$ref": reference}}},
                    },
                    f"$.components.schemas.{name}.$ref",
                ),
                (
                    _openapi_with_schema(
                        {
                            "type": "object",
                            "properties": {name: {"$ref": reference}},
                        }
                    ),
                    f"$.components.schemas.Thing.properties.{name}.$ref",
                ),
            )
            for document, location in documents:
                with self.subTest(name=name, location=location):
                    self.assertEqual(
                        _openapi_document_error(document),
                        location + REFERENCE_ERROR_TAIL,
                    )

    def test_naming_ref_somewhere_that_is_not_a_mapping_key_is_still_fine(self):
        """Premise: the rejection above is about the key, not the four bytes.

        A schema whose `required` array contains the string `$ref` is accepted,
        which shows the walk is looking at mapping keys rather than matching
        text anywhere in the document.
        """
        document = _openapi_with_schema({"type": "object", "required": ["$ref"]})
        self.assertIsNone(_openapi_document_error(document))


# ---------------------------------------------------------------------------
# OBL-HASHING-085: what a verification baseline may never acknowledge
# ---------------------------------------------------------------------------


def _safety_prefixes() -> Tuple[str, ...]:
    """Read the non-baselinable prefixes out of the exit-code classifier.

    `core._drift_exit_code` is where the product itself decides that a
    diagnostic is an integrity or usage failure rather than drift. Reading its
    literal with `ast` rather than restating it means a prefix added there
    without a row in `NON_BASELINABLE_ISSUES` fails the enumeration test.
    """
    tree = ast.parse(inspect.getsource(core._drift_exit_code))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "safety_prefixes"
            for target in node.targets
        ):
            continue
        return tuple(
            element.value
            for element in node.value.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        )
    raise AssertionError("core._drift_exit_code no longer defines safety_prefixes")


#: The two families that are non-baselinable without appearing in
#: `safety_prefixes`, because they are ordinary-severity drift diagnostics that
#: `violation_identity` still refuses to give an identity.
EXTRA_NON_BASELINABLE_PREFIXES = ("METADATA MISMATCH", "VENDORED DRIFT")

#: One representative diagnostic per non-baselinable prefix. The five
#: capitalised families carry text observed from a live `verify --format json`
#: (and `VENDORED DRIFT` carries the literal from `_lockfile.py`, which has no
#: live producer left - see the test that says so). The eight usage prefixes
#: carry a `svc.boundary:` tail, which is the subject-and-facet shape
#: `_COMPONENT_FACET_RE` reads. That tail is *not* adversarial against a change
#: of anchoring, and an earlier draft of this file claimed it was: the pattern
#: also requires the literal `MISMATCH `, which none of these strings contains,
#: so no anchoring change could make one of them match. The rows test one fact -
#: `violation_identity` gives no identity to a string that does not begin with
#: one of the three baselinable openings - and the strings that a de-anchored
#: pattern really would swallow are in `DE_ANCHORING_TRAPS` below.
NON_BASELINABLE_ISSUES = {
    "CURRENT DIGEST ERROR": (
        "CURRENT DIGEST ERROR svc: Declared boundary path matched no tracked "
        "files: missing.yaml"
    ),
    "LOCKED DIGEST ERROR": "LOCKED DIGEST ERROR svc: tracked tree unavailable at HEAD",
    "DERIVATION ERROR": (
        "DERIVATION ERROR svc.boundary: generated artifact evidence is stale"
    ),
    "UNAVAILABLE FACET": (
        "UNAVAILABLE FACET svc.compat: selected gate requires both locked and "
        "current digests"
    ),
    "DIAGNOSTICS TRUNCATED": DIAGNOSTIC_TRUNCATION_SENTINEL,
    "METADATA MISMATCH": (
        "METADATA MISMATCH config_digest: "
        "lockfile='a8ff13cdaa102cecbffa93496edf7f2ff4970f947d34d2021e6feab12d4817e0' "
        "current='f4982aa7fcef72f7b15427ad2ade1bc7962fc9e550c35d3ce447543e0f52482e'"
    ),
    "VENDORED DRIFT": (
        "VENDORED DRIFT svc: Vendored copy at 'vendor/svc' differs from source"
    ),
    "Config root": "Config root svc.boundary: crafted tail",
    "LOCKFILE": "LOCKFILE svc.boundary: crafted tail",
    "Custom provider loading failed": (
        "Custom provider loading failed svc.boundary: crafted tail"
    ),
    "Config invalid": "Config invalid svc.boundary: crafted tail",
    "Config unavailable": "Config unavailable svc.boundary: crafted tail",
    "Verification error": "Verification error svc.boundary: crafted tail",
    "Cannot capture": "Cannot capture svc.boundary: crafted tail",
    "Config malformed": "Config malformed svc.boundary: crafted tail",
    "Lockfile schema mismatch": (
        "Lockfile schema mismatch svc.boundary: crafted tail"
    ),
    "Unknown verification facet": (
        "Unknown verification facet svc.boundary: crafted tail"
    ),
    "Unknown verification component": (
        "Unknown verification component svc.boundary: crafted tail"
    ),
    "CONFIG SOURCE DIVERGENCE": (
        "CONFIG SOURCE DIVERGENCE svc.boundary: selected config differs from disk"
    ),
}

#: The context an `apply_baseline` unit call has to reproduce exactly. Only the
#: keys the caller passes are compared, so a two-field context is a complete
#: one for these tests; the end-to-end test uses the real nine-field context.
UNIT_CONTEXT = {"project": "scenario", "source": "head"}

#: A declared boundary path is echoed verbatim into `CURRENT DIGEST ERROR` and
#: into the `boundary_errors` metadata line. Both validators accept this one -
#: it is only a glob that matches nothing - so the text of a baselinable slice
#: identity can be put inside an integrity failure by editing the config.
SLICE_IDENTITY_PATH = "SLICE MISMATCH all.boundary: lockfile=a current=b"

#: Non-baselinable diagnostics that an identity regex would claim if it stopped
#: anchoring at the start of the string, mapped to the entry that claim would
#: build. This is the table `NON_BASELINABLE_ISSUES` was wrongly said to be: the
#: first row is crafted, and the other two were observed from a live
#: `verify --format json` against the repository the test below builds. Every
#: row is checked against the de-anchored pattern itself, so a row that stopped
#: being a trap fails rather than sitting here as decoration.
DE_ANCHORING_TRAPS = {
    "a facet-shaped tail on a metadata mismatch": (
        "METADATA MISMATCH svc.compat: lockfile='a' current='b'",
        _identity_entry("component-facet", "svc", "compat"),
    ),
    "an integrity failure quoting a slice identity": (
        "CURRENT DIGEST ERROR svc: Declared boundary path matched no tracked "
        f"files: {SLICE_IDENTITY_PATH}",
        _identity_entry("slice-facet", "all", "boundary"),
    ),
    "a metadata value quoting a slice identity": (
        "METADATA MISMATCH svc.boundary_errors: lockfile=None "
        "current=['Declared boundary path matched no tracked files: "
        f"{SLICE_IDENTITY_PATH}']",
        _identity_entry("slice-facet", "all", "boundary"),
    ),
}

#: Which pattern each trap kind belongs to, so the premise checks the regex the
#: trap is actually aimed at rather than whichever one happens to match.
IDENTITY_PATTERNS = {
    "component-facet": _COMPONENT_FACET_RE,
    "slice-facet": _SLICE_FACET_RE,
}


def _de_anchored(pattern: "re.Pattern") -> "re.Pattern":
    """The same pattern with its leading `^` removed.

    This is the regression the obligation's gap describes in concrete form. A
    string is a trap only if the de-anchored pattern claims it, and asserting
    that here means the traps below cannot quietly stop being traps the way the
    `svc.boundary:` tails did.
    """
    if not pattern.pattern.startswith("^"):
        raise AssertionError(f"pattern is no longer anchored: {pattern.pattern}")
    return re.compile(pattern.pattern[1:], pattern.flags)


def _subject_candidates(issue: str) -> List[str]:
    """Every subject a crafted entry could plausibly claim for *issue*.

    An attacker writing a baseline entry by hand does not have to guess: the
    diagnostic is right there in the CI log. These are the readings of it that
    a `component-facet` or `slice-facet` identity could be built from.
    """
    head = issue.split(":", 1)[0]
    words = head.split()
    candidates = {issue[:200], head, words[-1] if words else head}
    if words:
        tail = words[-1]
        candidates.add(tail.split(".")[0])
        candidates.add(tail.rsplit(".", 1)[0])
    candidates.add("svc")
    return sorted(candidate for candidate in candidates if candidate)


def _exhaustive_baseline(issue: str) -> dict:
    """A baseline holding every schema-valid entry that could target *issue*.

    The kinds and their permitted facets are read from `_KIND_FACETS` at
    runtime, so a kind or facet added to the baseline format is covered here
    without anyone remembering to add it.
    """
    entries: Dict[str, dict] = {}
    for kind, facets in _KIND_FACETS.items():
        for subject, facet in itertools.product(
            _subject_candidates(issue), sorted(facets)
        ):
            entry = _identity_entry(kind, subject, facet)
            entries[entry["id"]] = entry
    return {**UNIT_CONTEXT, "violations": list(entries.values())}


def _lock_and_commit(scene: Scenario) -> None:
    result = run_cli(scene.root, "generate")
    assert result.returncode == 0, result.stderr
    scene.git("add", "--all")
    scene.git("commit", "-m", "lock")


def _lock_and_commit_in_process(scene: Scenario) -> None:
    """The same setup without paying for an interpreter start-up.

    A generated property builds a repository per example, and a subprocess
    `generate` costs about 1.9 seconds against 0.7 in process. The lock this
    writes is produced by the same `_cmd_generate` either way; what the
    subprocess buys is process isolation, which the fixed corpus already pays
    for.
    """
    result = run_cli_in_process(scene.root, "generate", "--quiet")
    assert result.returncode == 0, result.stderr
    scene.git("add", "--all")
    scene.git("commit", "-m", "lock")


def _build_current_digest_error(scene: Scenario) -> None:
    scene.component("svc", path="svc", provider="path-hash", boundary=["api.yaml"])
    scene.file("svc/api.yaml", "openapi: 3.1.0\n")
    scene.commit()
    _lock_and_commit(scene)
    scene.config["components"]["svc"]["boundary"]["paths"] = ["missing.yaml"]
    scene.commit("break")


def _build_locked_digest_error(scene: Scenario) -> None:
    scene.component("svc", path="svc", provider="path-hash", boundary=["api.yaml"])
    scene.file("svc/api.yaml", "openapi: 3.1.0\n")
    scene.commit()
    result = run_cli(scene.root, "generate")
    assert result.returncode == 0, result.stderr
    lock_path = scene.root / "boundary.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["components"]["svc"]["exact_errors"] = ["tracked tree unavailable at HEAD"]
    lock_path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    scene.git("add", "--all")
    scene.git("commit", "-m", "tampered lock")


def _build_unavailable_facet(scene: Scenario) -> None:
    scene.component("svc", path="svc", provider="path-hash", boundary=["api.yaml"])
    scene.file("svc/api.yaml", "openapi: 3.1.0\n")
    scene.commit()
    _lock_and_commit(scene)
    scene.file("svc/api.yaml", "openapi: 3.1.0\n# drifted\n")
    scene.git("add", "--all")
    scene.git("commit", "-m", "drift")


def _build_truncated_diagnostics(scene: Scenario) -> None:
    for index in range(6):
        name = f"svc{index}"
        scene.component(name, path=name, provider="path-hash", boundary=["api.yaml"])
        scene.file(f"{name}/api.yaml", "openapi: 3.1.0\n")
    scene.commit()
    _lock_and_commit(scene)
    for index in range(6):
        scene.file(f"svc{index}/api.yaml", "openapi: 3.1.0\n# drifted\n")
    scene.git("add", "--all")
    scene.git("commit", "-m", "drift")


#: Repositories that really do produce a non-baselinable family, so the crafted
#: strings above can be checked against the product's own wording rather than
#: against a remembered version of it. `item_limit` lowers
#: `_utils.MAX_DIAGNOSTIC_ITEMS`, which is the only way to reach the truncation
#: sentinel without 256 real diagnostics, and forces the in-process runner
#: because a module constant cannot be lowered across a process boundary.
LIVE_FAILURE_ROWS = {
    "current digest error": (
        _build_current_digest_error,
        ("verify", "--format", "json"),
        "CURRENT DIGEST ERROR",
        None,
    ),
    "locked digest error": (
        _build_locked_digest_error,
        ("verify", "--format", "json"),
        "LOCKED DIGEST ERROR",
        None,
    ),
    "unavailable facet": (
        _build_unavailable_facet,
        ("verify", "--format", "json", "--facets", "boundary,compat"),
        "UNAVAILABLE FACET",
        None,
    ),
    "diagnostics truncated": (
        _build_truncated_diagnostics,
        ("verify", "--format", "json"),
        "DIAGNOSTICS TRUNCATED",
        4,
    ),
}


class NonBaselinableFailureTests(unittest.TestCase):
    """OBL-HASHING-085: no crafted baseline entry may acknowledge integrity."""

    def _assert_survives(self, issue: str) -> None:
        self.assertIsNone(violation_identity(issue), issue)
        baseline = _exhaustive_baseline(issue)
        new, acknowledged, _stale = apply_baseline(baseline, UNIT_CONTEXT, [issue])
        self.assertEqual(new, [issue])
        self.assertEqual(acknowledged, [])

    def test_the_table_covers_every_prefix_the_classifier_treats_as_a_safety_failure(
        self,
    ):
        derived = set(_safety_prefixes()) | set(EXTRA_NON_BASELINABLE_PREFIXES)
        self.assertEqual(set(NON_BASELINABLE_ISSUES), derived)
        for prefix, issue in NON_BASELINABLE_ISSUES.items():
            with self.subTest(prefix=prefix):
                self.assertTrue(issue.startswith(prefix))

    def test_no_crafted_entry_of_any_kind_or_facet_acknowledges_a_safety_failure(self):
        for prefix, issue in NON_BASELINABLE_ISSUES.items():
            with self.subTest(prefix=prefix, entries=len(_exhaustive_baseline(issue))):
                self._assert_survives(issue)

    def test_a_metadata_mismatch_on_every_recorded_field_stays_unacknowledgeable(self):
        """The metadata surface is read from the lockfile's own field tuple.

        `COMPONENT_METADATA_FIELDS` is what `verify_lockfile` iterates when it
        emits `METADATA MISMATCH`, so a field added there is covered here
        without anyone editing this test. `version` and `semver` are excluded
        and handled by the divergence tests below; every other field must be
        untouchable on its own.
        """
        for field in COMPONENT_METADATA_FIELDS:
            if field in {"version", "semver"}:
                continue
            with self.subTest(field=field):
                self._assert_survives(
                    f"METADATA MISMATCH svc.{field}: lockfile='a' current='b'"
                )

    def test_a_genuine_facet_mismatch_is_acknowledged_by_its_own_entry(self):
        """Premise: `apply_baseline` can move an issue out of the failure set.

        Every assertion above says an issue was *not* acknowledged. Without
        this, all of them would pass against an `apply_baseline` that never
        acknowledged anything, which is the failure this suite keeps producing.
        """
        rows = {
            "component-facet": (
                "MISMATCH svc.boundary: lockfile=aaaa... current=bbbb...",
                _identity_entry("component-facet", "svc", "boundary"),
            ),
            "slice-facet": (
                "SLICE MISMATCH all.boundary: lockfile=aaaa... current=bbbb...",
                _identity_entry("slice-facet", "all", "boundary"),
            ),
        }
        for kind, (issue, entry) in rows.items():
            with self.subTest(kind=kind):
                baseline = {**UNIT_CONTEXT, "violations": [entry]}
                new, acknowledged, stale = apply_baseline(
                    baseline, UNIT_CONTEXT, [issue]
                )
                self.assertEqual(new, [])
                self.assertEqual(acknowledged, [issue])
                self.assertEqual(stale, [])

    def test_a_crafted_entry_that_targets_nothing_is_reported_as_stale(self):
        """Premise: the crafted entries really do reach `apply_baseline`.

        A test that only asserted the issue survived could pass against a
        baseline whose entries were dropped before comparison. Every crafted id
        coming back as stale proves the entry set was read.
        """
        issue = NON_BASELINABLE_ISSUES["CURRENT DIGEST ERROR"]
        baseline = _exhaustive_baseline(issue)
        _new, _acknowledged, stale = apply_baseline(baseline, UNIT_CONTEXT, [issue])
        self.assertEqual(
            sorted(stale), sorted(entry["id"] for entry in baseline["violations"])
        )

    def test_the_crafted_tails_cannot_be_reached_by_a_de_anchored_regex_at_all(self):
        """A correction, kept as an assertion rather than as a promise.

        The `svc.boundary:` tails in `NON_BASELINABLE_ISSUES` were introduced as
        bait for an identity regex that stopped anchoring at the start of the
        string. They are not: both patterns also require the literal
        `MISMATCH `, which no usage-prefix diagnostic contains, so the anchor
        could be removed from either one without a single row noticing. Writing
        that down here keeps the table honest about which fact it tests, and the
        traps that do reach the patterns are asserted in the next test.
        """
        component = _de_anchored(_COMPONENT_FACET_RE)
        slice_pattern = _de_anchored(_SLICE_FACET_RE)
        for prefix, issue in NON_BASELINABLE_ISSUES.items():
            with self.subTest(prefix=prefix):
                self.assertIsNone(component.search(issue), issue)
                self.assertIsNone(slice_pattern.search(issue), issue)

    def test_a_diagnostic_a_de_anchored_regex_would_claim_is_still_refused(self):
        """The bait the table above does not carry, with its premise attached.

        Each row is first shown to be a real trap: the pattern its entry belongs
        to, with the `^` removed, matches the diagnostic and reads exactly the
        subject and facet written beside it. Only then is the product asserted to
        refuse it - no identity at all, and no acknowledgement even when the
        baseline holds precisely the entry that match would have built, which is
        the baseline a repository with a genuinely acknowledged slice mismatch
        would be carrying. The entry coming back as stale proves it was read.
        """
        for label, (issue, entry) in DE_ANCHORING_TRAPS.items():
            with self.subTest(trap=label):
                pattern = _de_anchored(IDENTITY_PATTERNS[entry["kind"]])
                match = pattern.search(issue)
                self.assertIsNotNone(match, issue)
                self.assertEqual(match.group("subject"), entry["subject"])
                self.assertEqual(match.group("facet"), entry["facet"])

                self.assertIsNone(violation_identity(issue), issue)
                new, acknowledged, stale = apply_baseline(
                    {**UNIT_CONTEXT, "violations": [entry]}, UNIT_CONTEXT, [issue]
                )
                self.assertEqual(new, [issue])
                self.assertEqual(acknowledged, [])
                self.assertEqual(stale, [entry["id"]])
                self._assert_survives(issue)

    def test_a_repository_really_can_put_a_slice_identity_inside_an_integrity_failure(
        self,
    ):
        """The trap above is reachable, not a shape invented for the test.

        A declared boundary path is arbitrary text as far as both validators are
        concerned, and `CURRENT DIGEST ERROR` quotes it back. So the two live
        rows of `DE_ANCHORING_TRAPS` are produced here rather than remembered,
        and both are put in front of `apply_baseline` beside a baseline holding
        the slice identity they spell out and the genuine component mismatch this
        run also produces. The genuine one is acknowledged in that same call,
        which is what proves the baseline was applied rather than ignored while
        the integrity failures stayed in the new set.
        """
        with Scenario() as scene:
            scene.component(
                "svc", path="svc", provider="path-hash", boundary=["api.yaml"]
            )
            scene.file("svc/api.yaml", "openapi: 3.1.0\n")
            scene.commit()
            _lock_and_commit(scene)
            scene.config["components"]["svc"]["boundary"]["paths"] = [
                SLICE_IDENTITY_PATH
            ]
            scene.commit("break")
            result = run_cli(scene.root, "verify", "--format", "json")
            self.assertTrue(result.stdout.strip(), result.stderr)
            issues = json.loads(result.stdout)["issues"]

        self.assertEqual(result.returncode, 2)
        genuine = "MISMATCH svc.boundary: lockfile=de5fd5cfd0a8... current=none"
        for label in (
            "an integrity failure quoting a slice identity",
            "a metadata value quoting a slice identity",
        ):
            with self.subTest(row=label):
                self.assertIn(DE_ANCHORING_TRAPS[label][0], issues)
        self.assertIn(genuine, issues)

        baseline = {
            **UNIT_CONTEXT,
            "violations": [
                _identity_entry("slice-facet", "all", "boundary"),
                _identity_entry("component-facet", "svc", "boundary"),
            ],
        }
        new, acknowledged, _stale = apply_baseline(baseline, UNIT_CONTEXT, issues)
        self.assertEqual(acknowledged, [genuine])
        for issue in issues:
            if issue.startswith(tuple(NON_BASELINABLE_ISSUES)):
                with self.subTest(issue=issue[:80]):
                    self.assertIn(issue, new)
                    self._assert_survives(issue)

    def test_the_live_diagnostics_for_each_family_are_still_unacknowledgeable(self):
        """The families produced by a real repository, not written down here.

        Each row runs `verify` against a repository built to fail that way and
        feeds every diagnostic it printed back through the crafted-baseline
        check, so this test fails if the product's wording moves away from the
        table above rather than passing on a stale string.
        """
        for label, (build, args, prefix, item_limit) in LIVE_FAILURE_ROWS.items():
            with self.subTest(family=label):
                with Scenario() as scene:
                    build(scene)
                    if item_limit is None:
                        result = run_cli(scene.root, *args)
                    else:
                        with mock.patch.object(
                            _utils, "MAX_DIAGNOSTIC_ITEMS", item_limit
                        ):
                            result = run_cli_in_process(scene.root, *args)
                    self.assertTrue(result.stdout.strip(), result.stderr)
                    issues = json.loads(result.stdout)["issues"]
                self.assertEqual(result.returncode, 2)
                matching = [issue for issue in issues if issue.startswith(prefix)]
                self.assertTrue(matching, issues)
                for issue in issues:
                    if issue.startswith(tuple(NON_BASELINABLE_ISSUES)):
                        with self.subTest(issue=issue[:80]):
                            self._assert_survives(issue)

    def test_vendored_drift_is_live_and_cannot_be_baselined(self):
        with Scenario() as scene:
            scene.component(
                "svc", path="svc", provider="path-hash", boundary=["api.yaml"]
            )
            scene.config["components"]["svc"]["vendored_copies"] = ["vendor/svc"]
            scene.file("svc/api.yaml", "openapi: 3.1.0\n")
            scene.file("vendor/svc/api.yaml", "openapi: 3.1.0\n")
            scene.commit()
            _lock_and_commit(scene)
            scene.file("vendor/svc/api.yaml", "openapi: 3.1.0\n# vendored drift\n")
            scene.git("add", "--all")
            scene.git("commit", "-m", "vendor drift")
            result = run_cli(scene.root, "verify", "--format", "json")
            issues = json.loads(result.stdout)["issues"]
        self.assertEqual(result.returncode, 1)
        self.assertTrue(
            [i for i in issues if i.startswith("VENDORED DRIFT svc:")], issues
        )
        self.assertFalse(
            [i for i in issues if i.startswith("CURRENT DIGEST ERROR svc: Vendored")],
            issues,
        )
        for issue in issues:
            with self.subTest(issue=issue[:80]):
                self._assert_survives(issue)

    def test_a_hand_edited_baseline_cannot_suppress_a_broken_repository(self):
        """The end-to-end shape the obligation is really about.

        A baseline is written legitimately while the only failure is drift, six
        crafted entries are appended to the committed file, and the repository
        is then broken so integrity failures appear. Every integrity failure has
        to stay in `issues`, none of them may appear in
        `baseline.baselined_issues`, and the exit code has to stay 2 - while the
        two genuine mismatches are still acknowledged, which is what proves the
        baseline was applied at all rather than ignored.
        """
        with Scenario() as scene:
            scene.component(
                "svc", path="svc", provider="path-hash", boundary=["api.yaml"]
            )
            scene.file("svc/api.yaml", "openapi: 3.1.0\n")
            scene.commit()
            _lock_and_commit(scene)
            scene.file("svc/api.yaml", "openapi: 3.1.0\n# drifted\n")
            scene.git("add", "--all")
            scene.git("commit", "-m", "drift")

            created = run_cli(
                scene.root,
                "verify",
                "--format",
                "json",
                "--write-baseline",
                "debt.json",
            )
            self.assertEqual(created.returncode, 0, created.stderr)

            baseline_path = scene.root / "debt.json"
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
            known = {entry["id"] for entry in baseline["violations"]}
            crafted = [
                _identity_entry("component-facet", "svc", "compat"),
                _identity_entry("component-facet", "svc", "behavior"),
                _identity_entry("component-facet", "config_digest", "exact"),
                _identity_entry("component-facet", "svc.boundary_status", "exact"),
                _identity_entry("slice-facet", "svc", "boundary"),
                _identity_entry("affected-consumers", "svc", "direct"),
            ]
            for entry in crafted:
                if entry["id"] not in known:
                    baseline["violations"].append(entry)
                    known.add(entry["id"])
            baseline["violations"].sort(key=lambda entry: entry["id"])
            baseline_path.write_text(
                json.dumps(baseline, indent=2) + "\n", encoding="utf-8"
            )
            scene.git("add", "--all")
            scene.git("commit", "-m", "hand-edited baseline")

            scene.config["components"]["svc"]["boundary"]["paths"] = ["missing.yaml"]
            scene.commit("break")

            result = run_cli(
                scene.root, "verify", "--format", "json", "--baseline", "debt.json"
            )
            payload = json.loads(result.stdout)

        self.assertEqual(result.returncode, 2)
        self.assertIs(payload["ok"], False)
        self.assertRegex(
            payload["issues"][0],
            r"^METADATA MISMATCH config_digest: lockfile='[0-9a-f]{64}' "
            r"current='[0-9a-f]{64}'$",
        )
        self.assertEqual(
            payload["issues"][1:],
            [
                "CURRENT DIGEST ERROR svc: Declared boundary path matched no "
                "tracked files: missing.yaml",
                "METADATA MISMATCH svc.boundary_status: lockfile='ok' "
                "current='error'",
                "METADATA MISMATCH svc.boundary_errors: lockfile=None "
                "current=['Declared boundary path matched no tracked files: "
                "missing.yaml']",
            ],
        )
        baselined = payload["baseline"]["baselined_issues"]
        for issue in payload["issues"]:
            self.assertNotIn(issue, baselined)
        self.assertEqual(
            [issue.split(":", 1)[0] for issue in baselined],
            ["MISMATCH svc.boundary", "MISMATCH svc.exact"],
        )
        self.assertEqual(
            sorted(payload["baseline"]["stale_ids"]),
            sorted(entry["id"] for entry in crafted),
        )

    def test_ancillary_version_metadata_is_acknowledged_only_beside_its_compat(self):
        """The current rule, pinned on both sides of its condition.

        Both sides means the field as well as the component. The rule is keyed
        on the component - `_ancillary_compat_subject` reads the subject out of
        the diagnostic and looks it up among the compat mismatches - and only
        the `version|semver` alternation in that pattern keeps it from carrying
        every other metadata field with it. So one case here holds an
        acknowledgeable line and an unacknowledgeable one for the same component
        in the same call: with `svc.compat` baselined and mismatching,
        `svc.version` is acknowledged and `svc.boundary_status` must not be. No
        other case in this file puts those two together, and without it a
        pattern broadened to any field would look exactly the same.
        """
        compat = "MISMATCH svc.compat: lockfile=aaaa... current=bbbb..."
        version = "METADATA MISMATCH svc.version: lockfile='1.0.0' current='2.0.0'"
        semver = (
            "METADATA MISMATCH svc.semver: lockfile={'compat_family': '1'} "
            "current={'compat_family': '2'}"
        )
        compat_entry = _identity_entry("component-facet", "svc", "compat")

        with self.subTest(case="compat baselined and currently mismatching"):
            new, acknowledged, _stale = apply_baseline(
                {**UNIT_CONTEXT, "violations": [compat_entry]},
                UNIT_CONTEXT,
                [compat, version, semver],
            )
            self.assertEqual(new, [])
            self.assertEqual(acknowledged, [compat, version, semver])

        with self.subTest(case="an integrity field beside an acknowledged version"):
            status = (
                "METADATA MISMATCH svc.boundary_status: "
                "lockfile='ok' current='error'"
            )
            new, acknowledged, _stale = apply_baseline(
                {**UNIT_CONTEXT, "violations": [compat_entry]},
                UNIT_CONTEXT,
                [compat, version, status],
            )
            self.assertEqual(new, [status])
            self.assertEqual(acknowledged, [compat, version])

        with self.subTest(case="compat baselined but not currently mismatching"):
            new, acknowledged, _stale = apply_baseline(
                {**UNIT_CONTEXT, "violations": [compat_entry]},
                UNIT_CONTEXT,
                [version, semver],
            )
            self.assertEqual(new, [version, semver])
            self.assertEqual(acknowledged, [])

        with self.subTest(case="compat currently mismatching but not baselined"):
            new, acknowledged, _stale = apply_baseline(
                {**UNIT_CONTEXT, "violations": []},
                UNIT_CONTEXT,
                [compat, version, semver],
            )
            self.assertEqual(new, [compat, version, semver])
            self.assertEqual(acknowledged, [])

        with self.subTest(case="a different component's compat does not carry it"):
            new, acknowledged, _stale = apply_baseline(
                {
                    **UNIT_CONTEXT,
                    "violations": [_identity_entry("component-facet", "dep", "compat")],
                },
                UNIT_CONTEXT,
                ["MISMATCH dep.compat: lockfile=a current=b", version],
            )
            self.assertIn(version, new)
            self.assertNotIn(version, acknowledged)

    def test_a_baselined_non_compat_facet_does_not_carry_ancillary_metadata(self):
        """The facet half of the forgiveness rule, which nothing pinned.

        `apply_baseline` collects `known_compat_subjects` from the baseline
        entries whose kind is `component-facet` and whose facet is `compat`.
        Every baseline in this suite that reaches that line already holds
        `svc.compat`, so the facet comparison could be dropped without a single
        assertion changing, which is what MUT-FACETS-402 does. Under that mutant
        a baseline forgiving only `svc.boundary` stands in for a compat identity
        nobody ever accepted, and the `svc.version` and `svc.semver` lines are
        acknowledged beside a live compat mismatch that is itself still reported
        as new. The test above fixes the subject half of the same condition and
        the run half of it, but never the facet half, because the entry it
        baselines is always a compat one.

        The entry baselined here names the same kind and the same subject as a
        compat entry and differs from it only in the facet, so the facet
        comparison is the only comparison that can reject it.
        """
        compat = "MISMATCH svc.compat: lockfile=aaaa... current=bbbb..."
        version = "METADATA MISMATCH svc.version: lockfile='1.0.0' current='2.0.0'"
        semver = (
            "METADATA MISMATCH svc.semver: lockfile={'compat_family': '1'} "
            "current={'compat_family': '2'}"
        )
        compat_entry = _identity_entry("component-facet", "svc", "compat")
        boundary_entry = _identity_entry("component-facet", "svc", "boundary")

        # PREMISE: the baselined entry really does differ from a compat entry in
        # its facet and in nothing else. Were its kind or its subject different
        # too, the refusal asserted below would be the work of one of those two
        # comparisons and would say nothing at all about the facet one.
        self.assertEqual(boundary_entry["kind"], compat_entry["kind"])
        self.assertEqual(boundary_entry["subject"], compat_entry["subject"])
        self.assertNotEqual(boundary_entry["facet"], compat_entry["facet"])
        self.assertNotEqual(boundary_entry["id"], compat_entry["id"])

        with self.subTest(case="a baselined boundary facet forgives nothing"):
            new, acknowledged, stale = apply_baseline(
                {**UNIT_CONTEXT, "violations": [boundary_entry]},
                UNIT_CONTEXT,
                [compat, version, semver],
            )
            self.assertEqual(acknowledged, [])
            self.assertEqual(new, [compat, version, semver])
            # The entry came back stale, which proves the baseline was read
            # rather than discarded before any of these comparisons ran.
            self.assertEqual(stale, [boundary_entry["id"]])

        with self.subTest(case="CONTRAST: the compat facet still carries them"):
            # A comparison that rejected every entry would satisfy the case
            # above and destroy the rule it guards, so the accepting case is
            # pinned here with the same three diagnostics and a baseline that
            # differs only by naming `compat` where the other named `boundary`.
            new, acknowledged, stale = apply_baseline(
                {**UNIT_CONTEXT, "violations": [compat_entry]},
                UNIT_CONTEXT,
                [compat, version, semver],
            )
            self.assertEqual(new, [])
            self.assertEqual(acknowledged, [compat, version, semver])
            self.assertEqual(stale, [])

    def test_a_baseline_written_for_another_scope_is_refused_outright(self):
        """Premise for every unit call above: the context really is compared.

        `apply_baseline` raises before it looks at a single violation when the
        recorded scope differs. If it did not, the two-field `UNIT_CONTEXT`
        would be proving nothing about the nine-field context the CLI builds.
        """
        with self.assertRaises(BaselineError) as caught:
            apply_baseline(
                {**UNIT_CONTEXT, "source": "index", "violations": []},
                UNIT_CONTEXT,
                [],
            )
        self.assertIn(
            "verification baseline source does not match this invocation",
            str(caught.exception),
        )


# ---------------------------------------------------------------------------
# OBL-PROVIDERS-059 / OBL-PROVIDERS-061: the embeddable API boundary
# ---------------------------------------------------------------------------


def _two_component_repository(scene: Scenario) -> None:
    """Two components, a consumer edge, a slice, and all four facets available.

    Every facet has to be *available* here, not just declared: an explicit
    `verify_facets` naming `compat` without a `version_source`, or `behavior`
    without `behavior.paths`, is a static config error, so a fixture without
    them would make half the generated invocations fail validation instead of
    verifying anything. The consumer edge exists so `--transitive` has
    something to widen, and the slice so slice drift is reachable.
    """
    for name in ("svc", "dep"):
        scene.component(
            name,
            path=name,
            provider="path-hash",
            boundary=["api.yaml"],
            behavior=["api.yaml", "pkg.json"],
            version_source={"file": "pkg.json", "field": "version"},
            consumers=["dep"] if name == "svc" else None,
        )
        scene.file(f"{name}/api.yaml", "openapi: 3.1.0\n")
        scene.json_file(f"{name}/pkg.json", {"version": "1.0.0"})
    scene.slice("all", mode="boundary", components=["svc", "dep"])
    scene.commit()


def _corpus_clean(scene: Scenario) -> None:
    _lock_and_commit(scene)


def _corpus_component_drift(scene: Scenario) -> None:
    _lock_and_commit(scene)
    scene.file("svc/api.yaml", "openapi: 3.1.0\n# drifted\n")
    scene.git("add", "--all")
    scene.git("commit", "-m", "drift")


def _corpus_slice_drift(scene: Scenario) -> None:
    _lock_and_commit(scene)
    scene.file("dep/api.yaml", "openapi: 3.1.0\n# drifted\n")
    scene.git("add", "--all")
    scene.git("commit", "-m", "slice drift")


def _corpus_preflight_failure(scene: Scenario) -> None:
    _lock_and_commit(scene)
    lock_path = scene.root / "boundary.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["components"].pop("dep")
    lock_path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    scene.git("add", "--all")
    scene.git("commit", "-m", "preflight")


def _corpus_provider_error(scene: Scenario) -> None:
    _lock_and_commit(scene)
    scene.config["components"]["svc"]["boundary"]["paths"] = ["missing.yaml"]
    scene.commit("break")


#: The five repository states the obligation names. Each is run through a real
#: `boundver` subprocess and through the in-process API, and the two `issues`
#: lists have to agree element for element and in order.
VERIFY_CORPUS = {
    "clean lock": _corpus_clean,
    "per-facet drift": _corpus_component_drift,
    "slice drift": _corpus_slice_drift,
    "preflight failure": _corpus_preflight_failure,
    "provider error": _corpus_provider_error,
}


class EmbeddableApiAgreementTests(unittest.TestCase):
    """OBL-PROVIDERS-059: `verify()` and `generate()` against the CLI."""

    def test_the_api_issue_list_matches_the_cli_json_across_the_corpus(self):
        observed = {}
        for label, prepare in VERIFY_CORPUS.items():
            with self.subTest(state=label):
                with Scenario() as scene:
                    _two_component_repository(scene)
                    prepare(scene)
                    cli = run_cli(scene.root, "verify", "--format", "json")
                    self.assertTrue(cli.stdout.strip(), cli.stderr)
                    cli_issues = json.loads(cli.stdout)["issues"]
                    with _inside(scene.root):
                        api_issues = boundver.verify()
                self.assertEqual(api_issues, cli_issues)
                observed[label] = cli_issues
        self.assertEqual(observed["clean lock"], [])
        for label in ("per-facet drift", "slice drift", "preflight failure",
                      "provider error"):
            with self.subTest(state=label, check="corpus is not degenerate"):
                self.assertTrue(observed[label])
        self.assertNotEqual(observed["per-facet drift"], observed["slice drift"])

    def test_the_api_lockfile_renders_to_the_bytes_the_cli_writes(self):
        for label, prepare in VERIFY_CORPUS.items():
            with self.subTest(state=label):
                with Scenario() as scene:
                    _two_component_repository(scene)
                    prepare(scene)
                    cli = run_cli(scene.root, "generate", "--format", "json")
                    written = (scene.root / "boundary.lock.json").read_text(
                        encoding="utf-8"
                    )
                    if cli.returncode != 0:
                        with _inside(scene.root):
                            with self.assertRaises(BoundverError):
                                boundver.generate(out_path=None)
                        continue
                    with _inside(scene.root):
                        document = boundver.generate(out_path=None)
                self.assertEqual(dump_lockfile(document), written)
                self.assertEqual(json.loads(cli.stdout), document)

    def test_the_api_lockfile_is_byte_identical_to_what_generate_prints(self):
        with Scenario() as scene:
            _two_component_repository(scene)
            _lock_and_commit(scene)
            printed = run_cli(scene.root, "generate", "--format", "json").stdout
            with _inside(scene.root):
                document = boundver.generate(out_path=None)
        self.assertEqual(dump_lockfile(document), printed)

    def test_the_two_renderings_have_identical_key_order(self):
        with Scenario() as scene:
            _two_component_repository(scene)
            _lock_and_commit(scene)
            printed = run_cli(scene.root, "generate", "--format", "json").stdout
            with _inside(scene.root):
                document = boundver.generate(out_path=None)
        rendered = dump_lockfile(document)
        self.assertEqual(rendered, printed)
        self.assertEqual(json.loads(printed), json.loads(rendered))
        self.assertEqual(list(json.loads(printed))[:2], ["$schema", "schema"])
        self.assertEqual(list(document)[:2], ["$schema", "schema"])

    def test_every_failing_api_call_raises_a_type_the_cli_handler_converts(self):
        """The exception taxonomy the CLI hides behind `_run_cli_handler`.

        `_run_cli_handler` turns `BoundverError`, `OSError` and a failed
        subprocess into `sys.exit(EXIT_USAGE)`; an embedder gets no such
        conversion and has to catch what escapes. Every row is pinned to its
        exact type rather than to a base class. Missing or misnamed lockfiles
        are normalized to the exported `LockfileError`. None of these calls may
        print, because a library write to stdout would corrupt its caller's own
        JSON.
        """
        rows = {
            "missing lockfile": (lambda scene: None, {}, LockfileError),
            "unknown lock path": (
                _corpus_clean,
                {"lock_path": "does/not/exist.json"},
                LockfileError,
            ),
            "lock inside a component": (
                _corpus_clean,
                {"lock_path": "svc/lock.json"},
                ConfigError,
            ),
            "missing config": (
                _corpus_clean,
                {"config_path": "absent.config.json"},
                ConfigError,
            ),
            "unknown source mode": (
                _corpus_clean,
                {"source": "nope"},
                ConfigError,
            ),
        }
        for label, (prepare, kwargs, expected) in rows.items():
            with self.subTest(case=label):
                with Scenario() as scene:
                    _two_component_repository(scene)
                    prepare(scene)
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with _inside(scene.root):
                        with contextlib.redirect_stdout(stdout):
                            with contextlib.redirect_stderr(stderr):
                                with self.assertRaises(expected) as caught:
                                    boundver.verify(**kwargs)
                self.assertIs(type(caught.exception), expected)
                self.assertIsInstance(caught.exception, (BoundverError, OSError))
                self.assertNotIsInstance(caught.exception, SystemExit)
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(stderr.getvalue(), "")

    def test_an_unknown_component_is_a_usage_error_to_both_surfaces(
        self,
    ):
        with Scenario() as scene:
            _two_component_repository(scene)
            _corpus_clean(scene)
            cli = run_cli(
                scene.root, "verify", "--format", "json", "--components", "nonexistent"
            )
            with _inside(scene.root):
                with self.assertRaises(ConfigError) as raised:
                    boundver.verify(components=["nonexistent"])
        self.assertEqual(cli.returncode, 2)
        self.assertEqual(cli.stdout, "")
        self.assertIn("unknown --components entries: nonexistent", cli.stderr)
        self.assertEqual(
            str(raised.exception),
            "Unknown verification component(s): nonexistent",
        )

    def test_a_valid_invocation_prints_nothing_either(self):
        """Premise: the silence above is not just an early exception.

        A failing call could be silent because it raised before reaching any
        output; a succeeding one that also prints nothing shows the API has no
        output path at all. That the buffers themselves work is the separate
        premise below, which fills both of them from the product.
        """
        with Scenario() as scene:
            _two_component_repository(scene)
            _corpus_component_drift(scene)
            stdout, stderr = io.StringIO(), io.StringIO()
            with _inside(scene.root):
                with contextlib.redirect_stdout(stdout):
                    with contextlib.redirect_stderr(stderr):
                        issues = boundver.verify()
        self.assertTrue(issues)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_the_redirection_those_silence_checks_use_captures_product_output(self):
        """Premise: an empty buffer means silence, not a buffer nobody wired up.

        Two tests above end in `assertEqual(stdout.getvalue(), "")`, which is an
        absence like any other, and the test they cite as their premise asserts
        an absence too. Here the identical nesting - `_inside`, then both
        redirections - is wrapped around `core.main()`, the product's own
        printing path: `verify --format json` fills the stdout buffer with the
        document it prints, and the same command with an unknown component fills
        the stderr buffer instead. Each buffer is shown to fill on its own, so
        an API call that wrote to either one would have been caught.
        """
        rows = {
            "a command that prints its report": (["verify", "--format", "json"], 4),
            "a command that prints a usage error": (
                ["verify", "--format", "json", "--components", "nonexistent"],
                2,
            ),
        }
        captured = {}
        with Scenario() as scene:
            _two_component_repository(scene)
            _corpus_component_drift(scene)
            for label, (args, expected_code) in rows.items():
                stdout, stderr = io.StringIO(), io.StringIO()
                with _inside(scene.root):
                    with contextlib.redirect_stdout(stdout):
                        with contextlib.redirect_stderr(stderr):
                            with mock.patch.object(sys, "argv", ["boundver", *args]):
                                with self.assertRaises(SystemExit) as caught:
                                    core.main()
                captured[label] = (
                    int(caught.exception.code or 0),
                    stdout.getvalue(),
                    stderr.getvalue(),
                )
                self.assertEqual(captured[label][0], expected_code)

        code, stdout_text, stderr_text = captured["a command that prints its report"]
        self.assertIn("issues", json.loads(stdout_text))
        self.assertEqual(stderr_text, "")

        code, stdout_text, stderr_text = captured["a command that prints a usage error"]
        self.assertEqual(stdout_text, "")
        self.assertEqual(
            stderr_text, "ERROR: unknown --components entries: nonexistent\n"
        )

    @given(
        drift=st.lists(st.sampled_from(("svc", "dep")), unique=True, max_size=2),
        facets=st.one_of(
            st.none(),
            st.lists(
                st.sampled_from(sorted(FACET_SET)), min_size=1, max_size=4, unique=True
            ).map(sorted),
        ),
        components=st.one_of(
            st.none(),
            st.lists(
                st.sampled_from(("svc", "dep")), min_size=1, max_size=2, unique=True
            ).map(sorted),
        ),
        transitive=st.booleans(),
        source=st.sampled_from(SOURCE_MODES),
        defaults=st.one_of(
            st.none(),
            st.lists(
                st.sampled_from(sorted(FACET_SET)), min_size=1, max_size=4, unique=True
            ).map(sorted),
        ),
    )
    @PROFILE
    def test_every_generated_invocation_agrees_between_the_api_and_the_cli(
        self, drift, facets, components, transitive, source, defaults
    ):
        """The obligation is over invocations, not over one of them.

        The oracle is the CLI's own JSON, produced by `core.main()` through
        argument parsing and `_cmd_verify` - a different code path from
        `boundver.verify()`, which is the surface where a divergence would
        live. Process isolation is covered by the five-member subprocess corpus
        above; this runs in-process so the space of invocations can be walked
        at all.
        """
        with Scenario() as scene:
            _two_component_repository(scene)
            if defaults is not None:
                scene.defaults(verify_facets=defaults)
                scene.commit("defaults")
            _lock_and_commit_in_process(scene)
            for name in drift:
                scene.file(f"{name}/api.yaml", "openapi: 3.1.0\n# drifted\n")
            if drift:
                scene.git("add", "--all")
                scene.git("commit", "-m", "drift")

            args = ["verify", "--format", "json", "--source", source]
            if facets is not None:
                args += ["--facets", ",".join(facets)]
            if components is not None:
                args += ["--components", ",".join(components)]
            if transitive:
                args.append("--transitive")
            cli = run_cli_in_process(scene.root, *args)
            with _inside(scene.root):
                api_issues = boundver.verify(
                    source=source,
                    facets=facets,
                    components=components,
                    transitive_consumers=transitive,
                )
        self.assertTrue(cli.stdout.strip(), cli.stderr)
        self.assertEqual(api_issues, json.loads(cli.stdout)["issues"])


class GenerateLeavesTheTreeAloneTests(unittest.TestCase):
    """OBL-PROVIDERS-061: `out_path=None`, and every rejected destination."""

    def test_out_path_none_leaves_the_tree_unchanged_for_every_mode_and_flag(self):
        with Scenario() as scene:
            _two_component_repository(scene)
            for source, custom in itertools.product(SOURCE_MODES, (False, True)):
                with self.subTest(source=source, allow_custom_providers=custom):
                    before = _tree_snapshot(scene.root)
                    status_before = scene.git("status", "--porcelain")
                    with _inside(scene.root):
                        document = boundver.generate(
                            out_path=None,
                            source=source,
                            allow_custom_providers=custom,
                        )
                    self.assertEqual(_tree_snapshot(scene.root), before)
                    self.assertEqual(scene.git("status", "--porcelain"), status_before)
                    self.assertEqual(sorted(document["components"]), ["dep", "svc"])

    def test_the_snapshot_notices_the_lockfile_a_real_generate_writes(self):
        """Premise: the comparison above can fail.

        `out_path="boundary.lock.json"` is the same call with the guard
        satisfied. If this did not show a new key, every no-write assertion in
        this class would be passing against a snapshot that saw nothing.
        """
        with Scenario() as scene:
            _two_component_repository(scene)
            before = _tree_snapshot(scene.root)
            with _inside(scene.root):
                boundver.generate(out_path="boundary.lock.json")
            after = _tree_snapshot(scene.root)
        self.assertEqual(
            [key for key in after if key not in before], ["boundary.lock.json"]
        )

    def test_a_rejected_destination_writes_nothing_and_creates_no_directory(self):
        """`_ensure_lock_outside_components` runs before generation on purpose.

        The check is ordered ahead of `generate_lockfile`, so a rejected
        destination must leave no lockfile, no temporary file from the atomic
        writer, and not even the parent directory the path implies.
        """
        rows = {
            "inside a component root": "svc/lock.json",
            "below a component root": "svc/deep/nested/lock.json",
            "inside the other component": "dep/lock.json",
        }
        for label, destination in rows.items():
            with self.subTest(case=label, out_path=destination):
                with Scenario() as scene:
                    _two_component_repository(scene)
                    before = _tree_snapshot(scene.root)
                    with _inside(scene.root):
                        with self.assertRaises(ConfigError) as caught:
                            boundver.generate(out_path=destination)
                    after = _tree_snapshot(scene.root)
                self.assertIn("is inside component root", str(caught.exception))
                self.assertEqual(after, before)

    def test_a_failing_generation_leaves_no_partial_lockfile_behind(self):
        rows = {
            "provider error": (
                lambda scene: scene.config["components"]["svc"]["boundary"]
                .__setitem__("paths", ["missing.yaml"]),
                "Lockfile generation failed",
            ),
            "invalid config": (
                lambda scene: scene.config["components"]["svc"].__setitem__(
                    "misspelled", 1
                ),
                "Config is invalid",
            ),
        }
        for label, (mutate, expected) in rows.items():
            with self.subTest(case=label):
                with Scenario() as scene:
                    _two_component_repository(scene)
                    mutate(scene)
                    scene.commit("break")
                    before = _tree_snapshot(scene.root)
                    with _inside(scene.root):
                        with self.assertRaises(ConfigError) as caught:
                            boundver.generate(out_path="boundary.lock.json")
                    after = _tree_snapshot(scene.root)
                self.assertIn(expected, str(caught.exception))
                self.assertEqual(after, before)
                self.assertNotIn("boundary.lock.json", after)


# ---------------------------------------------------------------------------
# OBL-CONFIG-010: the hand validator must not be weaker than the schema
# ---------------------------------------------------------------------------


def _base_config(scene: Scenario) -> dict:
    scene.component("svc", path="svc", provider="path-hash", boundary=["api.yaml"])
    scene.component("dep", path="dep", provider="path-hash", boundary=["api.yaml"])
    scene.file("svc/api.yaml", "openapi: 3.1.0\n")
    scene.file("dep/api.yaml", "openapi: 3.1.0\n")
    scene.json_file("svc/pkg.json", {"version": "1.0.0"})
    scene.commit()
    return copy.deepcopy(scene.config)


def _component(config: dict, name: str = "svc") -> dict:
    """The named component, or a scratch dict when a mutation removed it.

    Mutations compose, and `no components` deletes the map the other component
    rows write into. Handing those rows a throwaway dict makes such a pair
    collapse to the stronger of the two instead of raising inside the corpus
    builder, which would turn a generated composition into a test error rather
    than a verdict.
    """
    components = config.get("components")
    if isinstance(components, dict) and isinstance(components.get(name), dict):
        return components[name]
    return {}


def _boundary(config: dict) -> dict:
    """The svc boundary declaration, on the same terms as `_component`."""
    boundary = _component(config).get("boundary")
    return boundary if isinstance(boundary, dict) else {}


#: Near-miss mutations, each aimed at a rule that lives in the packaged JSON
#: Schema. A rule enforced only there is unenforced in the standalone zipapp,
#: where `_schema_engine_errors` returns `[]`; a rule enforced only by hand
#: makes an editor-clean config fail in CI. Each takes the config and one
#: generated token so the same row can be tried with many spellings, and the
#: generated property composes up to three of them, so every row has to survive
#: being applied after any other.
CONFIG_MUTATIONS = {
    "none": lambda config, token: None,
    "duplicate vendored_copies": lambda config, token: _component(
        config
    ).__setitem__("vendored_copies", ["dep", "dep"]),
    "vendored_copies token": lambda config, token: _component(config).__setitem__(
        "vendored_copies", [token, token]
    ),
    "slice name token": lambda config, token: config.__setitem__(
        "slices", {token: {"mode": "boundary", "components": ["svc"]}}
    ),
    "component name token": lambda config, token: config.setdefault(
        "components", {}
    ).__setitem__(token, copy.deepcopy(_component(config))),
    "component path token": lambda config, token: _component(config).__setitem__(
        "path", token
    ),
    "boundary path token": lambda config, token: _boundary(config).__setitem__(
        "paths", [token]
    ),
    "provider name token": lambda config, token: config.__setitem__(
        "providers", [{"module": "m", "class": "C", "name": token}]
    ),
    "git tag prefix token": lambda config, token: _component(config).__setitem__(
        "version_source", {"git_tag_prefix": token}
    ),
    "version field token": lambda config, token: _component(config).__setitem__(
        "version_source", {"file": "pkg.json", "field": token}
    ),
    "consumer token": lambda config, token: _component(config).__setitem__(
        "consumers", [token]
    ),
    "ecosystem is a number": lambda config, token: _component(config).__setitem__(
        "ecosystem", 7
    ),
    "note is a number": lambda config, token: _component(config).__setitem__("note", 7),
    "boundary note is a number": lambda config, token: _boundary(config).__setitem__(
        "note", 7
    ),
    "schema field is a number": lambda config, token: config.__setitem__("$schema", 7),
    "empty verify_facets": lambda config, token: config.__setitem__(
        "defaults", {"verify_facets": []}
    ),
    "duplicate verify_facets": lambda config, token: config.__setitem__(
        "defaults", {"verify_facets": ["exact", "exact"]}
    ),
    "unknown compat_mode": lambda config, token: config.__setitem__(
        "defaults", {"compat_mode": token}
    ),
    "boundary options is a list": lambda config, token: _boundary(config).__setitem__(
        "options", []
    ),
    "both version_source shapes": lambda config, token: _component(
        config
    ).__setitem__(
        "version_source",
        {"file": "pkg.json", "field": "version", "git_tag_prefix": "v"},
    ),
    "empty version_source": lambda config, token: _component(config).__setitem__(
        "version_source", {}
    ),
    "slice with neither member key": lambda config, token: config.__setitem__(
        "slices", {"s": {"mode": "boundary"}}
    ),
    "slice with both member keys": lambda config, token: config.__setitem__(
        "slices",
        {"s": {"mode": "boundary", "components": ["svc"], "closure_of": "svc"}},
    ),
    "empty slice members": lambda config, token: config.__setitem__(
        "slices", {"s": {"mode": "boundary", "components": []}}
    ),
    "unknown slice mode": lambda config, token: config.__setitem__(
        "slices", {"s": {"mode": token, "components": ["svc"]}}
    ),
    "project is empty": lambda config, token: config.__setitem__("project", ""),
    "no components": lambda config, token: config.__setitem__("components", {}),
    "unknown top-level field": lambda config, token: config.__setitem__("extra", 1),
    "unknown component field": lambda config, token: _component(config).__setitem__(
        "extra", 1
    ),
    "boundary paths is a string": lambda config, token: _boundary(config).__setitem__(
        "paths", "api.yaml"
    ),
    "too many providers": lambda config, token: config.__setitem__(
        "providers",
        [{"module": f"m{i}", "class": "C", "name": f"custom.p{i}"} for i in range(101)],
    ),
}


#: Spellings that sit exactly on a schema boundary: whitespace-only names, path
#: traversal, separators the pattern forbids, and the two custom-provider
#: prefixes. Hypothesis also draws free text, but these are the ones a random
#: draw would almost never produce.
CONFIG_TOKENS = (
    "",
    " ",
    "   ",
    "\t",
    "\n",
    "svc ",
    " svc",
    "a,b",
    "./svc",
    "../svc",
    "svc//x",
    "svc\\x",
    "C:svc",
    "/svc",
    "svc/",
    ".",
    "..",
    "custom.",
    "custom.thing ",
    "mine.thing",
    "v 1",
    "a..b",
    "refs/tags/",
    "x" * 16385,
)


def _validate_twice(
    config: dict, repo_root: Path, source: str = "head"
) -> Tuple[List[str], List[str]]:
    """Validate with the schema engine available, and with it unimportable.

    `_schema_engine_errors` catches `ImportError` and returns `[]`, which is
    the state the standalone zipapp is permanently in. Setting the module entry
    to None makes `import jsonschema` raise exactly that, and `mock.patch.dict`
    restores `sys.modules` afterwards.

    Both calls always use the same *source*, so the source-dependent
    path-existence rules cannot be the thing that separates them; the default
    is `head` because `working-tree` re-captures a Git index snapshot on every
    call and costs three times as much for a question that does not depend on
    it.
    """
    with_engine = validate_config(config, repo_root, source=source)
    with mock.patch.dict(sys.modules, {"jsonschema": None}):
        without_engine = validate_config(config, repo_root, source=source)
    return with_engine, without_engine


class SchemaAndHandValidatorParityTests(unittest.TestCase):
    """OBL-CONFIG-010: accept(schema) must equal accept(hand)."""

    @classmethod
    def setUpClass(cls):
        cls._scene = Scenario()
        cls.template = _base_config(cls._scene)
        cls.root = cls._scene.root

    @classmethod
    def tearDownClass(cls):
        cls._scene.close()

    def _mutated(self, names, token: str) -> dict:
        config = copy.deepcopy(self.template)
        for name in names:
            CONFIG_MUTATIONS[name](config, token)
        return config

    def test_the_schema_engine_reaches_validate_config_and_the_patch_removes_it(self):
        """Premise for every parity assertion below, taken where they take it.

        The property compares two calls to `validate_config` that are supersets
        of one another, so it can only ever fail when the schema rejects
        something the hand validator accepts. That makes it silent about the one
        state this obligation is really about: an engine that never reaches
        `validate_config` at all, which is what a standalone zipapp has. Then
        both sides are the hand validator and the parity is perfect for the
        worst possible reason. Checking `_schema_engine_errors` on its own does
        not rule that out, so the split is asserted through `validate_config`
        itself: an empty slice key, which `propertyNames.minLength` refuses and
        the hand validator refuses in its own words, must come back as two lines
        with jsonschema importable and as one without it.

        Note what this cannot cover. A rule that lives only in the hand
        validator - the editor-clean config that fails in CI - leaves both
        verdicts identical, so the property is one-directional and half of the
        obligation's motivation is out of its reach by construction.
        """
        config = copy.deepcopy(self.template)
        config["slices"] = {"": {"mode": "boundary", "components": ["svc"]}}
        schema_line = "Schema validation error at slices: '' should be non-empty"
        hand_line = "Slice names must be non-empty strings"

        with_engine, without_engine = _validate_twice(config, self.root)
        self.assertEqual(with_engine, [schema_line, hand_line])
        self.assertEqual(without_engine, [hand_line])

        schema = _load_config_schema(self.root)
        self.assertIsNotNone(schema)
        self.assertEqual(_schema_engine_errors(config, schema), [schema_line])
        with mock.patch.dict(sys.modules, {"jsonschema": None}):
            self.assertEqual(_schema_engine_errors(config, schema), [])

    def test_two_of_the_three_named_suspects_are_rejected_by_the_hand_validator_too(
        self,
    ):
        """The obligation names three rules it believes live only in the schema.

        Two of them are enforced by hand as well, so they are stale suspects
        rather than gaps. They are kept as rows because they are the cases a
        future edit is most likely to move into the schema alone. The third,
        `maxItems` on a slice's member list, is not here: it really is
        schema-only, and the test below is about why that is still not a hole.
        A row asserting the duplicates message against a 10001-entry list of
        repeats used to stand in for it, which tested the length cap not at all.
        """
        rows = {
            "duplicate vendored_copies": (
                {"components": {"svc": {"vendored_copies": ["dep", "dep"]}}},
                "Component 'svc' field 'vendored_copies' contains duplicates",
            ),
            "whitespace-only slice name": (
                {"slices": {"   ": {"mode": "boundary", "components": ["svc"]}}},
                "Slice names must be non-empty strings",
            ),
        }
        for label, (patch, expected) in rows.items():
            with self.subTest(suspect=label):
                config = copy.deepcopy(self.template)
                for key, value in patch.items():
                    if key == "components":
                        for name, fields in value.items():
                            config["components"][name].update(fields)
                    else:
                        config[key] = value
                with_engine, without_engine = _validate_twice(
                    config, self.root, source="working-tree"
                )
                self.assertIn(expected, without_engine)
                self.assertTrue(with_engine)

    def test_the_slice_length_cap_is_schema_only_and_cannot_be_reached_alone(self):
        """The obligation's third suspect: real, and harmless for a stated reason.

        `slices.<s>.components` carries `maxItems` and the hand validator has no
        length rule anywhere, so this one is not a stale suspect - a member list
        one entry over the cap draws exactly one schema error and not a single
        hand error about length. It is still not a parity hole, and the argument
        is arithmetic rather than hopeful. A config may declare at most
        `MAX_CONSUMER_GRAPH_ITEMS` components, the schema says the same in
        `components.maxProperties`, and the slice cap is no smaller than either,
        so a list long enough to trip the cap has to repeat a name or name a
        component that does not exist. The hand validator rejects both, and both
        are checked below. Should anyone lower the slice cap under the component
        limit, the first assertion fails - which is the point, because the gap
        would then be reachable by a config the hand validator accepts.
        """
        schema = _load_config_schema(self.root)
        cap = schema["properties"]["slices"]["additionalProperties"]["properties"][
            "components"
        ]["maxItems"]
        declarable = schema["properties"]["components"]["maxProperties"]
        self.assertEqual(declarable, MAX_CONSUMER_GRAPH_ITEMS)
        self.assertGreaterEqual(cap, declarable)

        at_cap = copy.deepcopy(self.template)
        at_cap["slices"] = {
            "big": {"mode": "boundary", "components": [f"c{i}" for i in range(cap)]}
        }
        self.assertEqual(_schema_engine_errors(at_cap, schema), [])

        over_cap = copy.deepcopy(self.template)
        over_cap["slices"] = {
            "big": {
                "mode": "boundary",
                "components": [f"c{i}" for i in range(cap + 1)],
            }
        }
        engine_errors = _schema_engine_errors(over_cap, schema)
        self.assertEqual(len(engine_errors), 1)
        self.assertTrue(
            engine_errors[0].startswith(
                "Schema validation error at slices.big.components: ['c0', 'c1',"
            ),
            engine_errors[0],
        )

        rows = {
            "distinct names over the cap": (
                [f"c{i}" for i in range(cap + 1)],
                "Slice 'big' references unknown component: c0",
            ),
            "repeated names over the cap": (
                ["svc"] * (cap + 1),
                "Slice 'big' field 'components' contains duplicates",
            ),
        }
        for label, (members, expected) in rows.items():
            with self.subTest(case=label):
                config = copy.deepcopy(self.template)
                config["slices"] = {"big": {"mode": "boundary", "components": members}}
                with_engine, without_engine = _validate_twice(config, self.root)
                self.assertIn(expected, without_engine)
                self.assertEqual(
                    [
                        error
                        for error in without_engine
                        if "too long" in error or str(cap) in error
                    ],
                    [],
                )
                self.assertEqual(not with_engine, not without_engine)

    def test_every_directed_near_miss_is_accepted_or_rejected_by_both_layers(self):
        for name in CONFIG_MUTATIONS:
            for token in ("   ", "custom.", "svc\\x"):
                with self.subTest(mutation=name, token=repr(token)[:24]):
                    config = self._mutated([name], token)
                    with_engine, without_engine = _validate_twice(config, self.root)
                    self.assertEqual(
                        not with_engine,
                        not without_engine,
                        f"schema-only verdict: {with_engine[:3]}",
                    )

    def test_an_unmutated_config_is_accepted_by_both_layers(self):
        """Premise: the corpus contains something both layers accept.

        Parity over a corpus that every layer rejects would be worthless - two
        validators that reject everything agree perfectly.
        """
        with_engine, without_engine = _validate_twice(
            copy.deepcopy(self.template), self.root
        )
        self.assertEqual(with_engine, [])
        self.assertEqual(without_engine, [])

    @given(
        names=st.lists(
            st.sampled_from(sorted(CONFIG_MUTATIONS)),
            min_size=1,
            max_size=3,
            unique=True,
        ),
        token=st.one_of(
            st.sampled_from(CONFIG_TOKENS),
            st.text(max_size=12),
            st.text(alphabet=" \t\n\r\u00a0\u2000\u3000", max_size=4),
        ),
        source=st.sampled_from(SOURCE_MODES),
    )
    @CONFIG_PROFILE
    def test_the_two_validation_layers_accept_exactly_the_same_configs(
        self, names, token, source
    ):
        """The obligation is a set equality, so the corpus has to be generated.

        Every existing parity test is one-directional or a hand-picked pair.
        Here the same config object is validated twice - once with jsonschema
        importable, once with the import failing - and only the accept/reject
        verdicts are compared, because the two layers are not required to word
        their errors the same way, only to agree on what is legal. The source
        mode is drawn as well: both calls in a pair share it, so it can never
        be what splits them, but a rule that only fires under one source is
        still reached.
        """
        config = self._mutated(names, token)
        with_engine, without_engine = _validate_twice(config, self.root, source=source)
        self.assertEqual(
            not with_engine,
            not without_engine,
            f"mutations={names} token={token!r} source={source} "
            f"schema-only={with_engine[:3]}",
        )


if __name__ == "__main__":
    unittest.main()
