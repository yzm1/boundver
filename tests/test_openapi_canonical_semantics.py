"""What the canonical form keeps, and where it decides that by the key name.

Canonicalising an OpenAPI document means dropping the parts that document the
API without constituting it - descriptions, summaries, examples. That is a
statement about positions in the document, not about spellings: `description`
is an annotation where a schema expects one, and an ordinary member name
inside an `enum` value, a `const`, or a `default`, where the document is
carrying data rather than describing it.

The same confusion runs the other way for `security`. A Security Requirement
object's keys are scheme names the author chose, so they must survive; an
array that merely sits under a key someone spelled `security` is not one.

Covers OBL-PROVIDERS-002, OBL-PROVIDERS-004 and OBL-PROVIDERS-005.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from boundver.providers import OpenApiCanonicalProvider, ProviderContext

PROFILE = settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


def _context(raw: bytes) -> ProviderContext:
    files = {"svc/api.openapi": raw}

    def read_file(path: str) -> bytes:
        return files[path]

    def read_file_limited(path: str, max_bytes: int) -> bytes:
        return files[path]

    def list_files(prefix: str):
        return sorted(name for name in files if name.startswith(prefix))

    return ProviderContext(
        repo_root=Path("."),
        component_path="svc",
        boundary_cfg={"paths": ["api.openapi"]},
        source="head",
        read_file=read_file,
        read_file_limited=read_file_limited,
        list_files=list_files,
    )


def _canonical(document: dict) -> bytes:
    """The canonical bytes for a document, or an assertion-friendly failure."""
    resolved = OpenApiCanonicalProvider().resolve(
        _context(json.dumps(document).encode("utf-8"))
    )
    if resolved.status != "ok" or not resolved.entries:
        raise AssertionError(f"resolve failed: {resolved.status} {resolved.errors}")
    return resolved.entries[0][1]


def _document(**extra) -> dict:
    """A minimal valid OpenAPI document with room for one experiment."""
    base = {
        "openapi": "3.1.0",
        "info": {"title": "t", "version": "1"},
        "paths": {"/a": {"get": {"responses": {"200": {"description": "ok"}}}}},
    }
    base.update(extra)
    return base


def _schema(**members) -> dict:
    return _document(components={"schemas": {"S": dict(members)}})


def _distinguishes(build) -> bool:
    """Does the canonical form tell two documents apart?"""
    return _canonical(build("a")) != _canonical(build("b"))


class DataValuedPositionTests(unittest.TestCase):
    """OBL-PROVIDERS-002: a key name is not a position."""

    def test_scalar_members_of_an_enum_are_compared(self):
        """The premise: enum values do reach the canonical form."""
        self.assertTrue(
            _distinguishes(lambda value: _schema(enum=["shared", value]))
        )

    def test_an_object_enum_member_keeps_its_own_keys(self):
        self.assertTrue(
            _distinguishes(lambda value: _schema(enum=[{"description": value}]))
        )

    def test_a_const_object_keeps_its_own_keys(self):
        self.assertTrue(
            _distinguishes(lambda value: _schema(const={"description": value}))
        )

    def test_a_default_object_keeps_its_own_keys(self):
        self.assertTrue(
            _distinguishes(lambda value: _schema(default={"description": value}))
        )
        self.assertTrue(
            _distinguishes(
                lambda value: _schema(default={"nested": {"description": value}})
            )
        )


class DataValuedScopeTests(unittest.TestCase):
    """Exactly which names vanish, so a partial fix cannot pass unnoticed."""

    #: Every candidate the test measures, annotation-ish or not.
    CANDIDATES = (
        "description", "summary", "example", "externalDocs",
        "title", "deprecated", "type", "format", "s", "x-note",
    )

    def _stripped_inside_an_enum(self):
        return tuple(
            key for key in self.CANDIDATES
            if not _distinguishes(lambda value, key=key: _schema(enum=[{key: value}]))
        )

    def test_the_names_that_vanish_from_a_data_value(self):
        self.assertEqual(self._stripped_inside_an_enum(), ())

    def test_the_names_that_survive(self):
        stripped = set(self._stripped_inside_an_enum())
        survivors = [key for key in self.CANDIDATES if key not in stripped]
        self.assertEqual(survivors, list(self.CANDIDATES))

    def test_the_member_object_and_its_data_are_preserved(self):
        canonical = _canonical(_schema(enum=[{"description": "a"}]))
        self.assertIn(b'"enum":[{"description":"a"}]', canonical)

    def test_default_is_opaque_only_inside_a_schema_object(self):
        document = _document(
            paths={
                "/a": {
                    "get": {
                        "responses": {
                            "default": {
                                "description": "response docs",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "description": "schema docs",
                                            "default": {
                                                "description": "contract data"
                                            },
                                        }
                                    }
                                },
                            }
                        }
                    }
                }
            }
        )
        canonical = json.loads(_canonical(document))
        response = canonical["paths"]["/a"]["get"]["responses"]["default"]
        self.assertNotIn("description", response)
        schema = response["content"]["application/json"]["schema"]
        self.assertNotIn("description", schema)
        self.assertEqual(schema["default"], {"description": "contract data"})

    def test_component_example_value_is_opaque_data(self):
        payload = {
            "$ref": "literal-user-data",
            "description": "contract data",
            "summary": "also contract data",
        }
        document = _document(
            components={"examples": {"payload": {"value": payload}}}
        )

        canonical = json.loads(_canonical(document))

        self.assertEqual(
            canonical["components"]["examples"]["payload"]["value"],
            payload,
        )
        self.assertTrue(
            _distinguishes(
                lambda value: _document(
                    components={"examples": {"payload": {
                        "value": {"description": value}
                    }}}
                )
            )
        )

    def test_inline_example_values_are_opaque_data(self):
        payload = {
            "$ref": "literal-user-data",
            "description": "example data",
            "summary": "also example data",
        }
        cases = (
            (
                "media type",
                _document(paths={"/a": {"get": {"responses": {"200": {
                    "description": "ok",
                    "content": {"application/json": {"examples": {
                        "payload": {"value": payload}
                    }}},
                }}}}}),
            ),
            (
                "parameter",
                _document(paths={"/a": {"get": {
                    "parameters": [{
                        "name": "q",
                        "in": "query",
                        "examples": {"payload": {"value": payload}},
                    }],
                    "responses": {"200": {"description": "ok"}},
                }}}),
            ),
            (
                "header",
                _document(paths={"/a": {"get": {"responses": {"200": {
                    "description": "ok",
                    "headers": {"X-Result": {
                        "schema": {"type": "string"},
                        "examples": {"payload": {"value": payload}},
                    }},
                }}}}}),
            ),
        )

        for label, document in cases:
            with self.subTest(label=label):
                # Literal `$ref` members are example payload data, not OpenAPI
                # Reference Objects, so validation must not reject them.
                _canonical(document)

    def test_singular_example_values_are_opaque_data(self):
        payload = {
            "$ref": "literal-user-data",
            "description": "example data",
        }
        cases = (
            (
                "schema",
                _schema(type="object", example=payload),
            ),
            (
                "schema examples",
                _schema(type="object", examples=[payload]),
            ),
            (
                "media type",
                _document(paths={"/a": {"get": {"responses": {"200": {
                    "description": "ok",
                    "content": {"application/json": {"example": payload}},
                }}}}}),
            ),
            (
                "parameter",
                _document(paths={"/a": {"get": {
                    "parameters": [{
                        "name": "q",
                        "in": "query",
                        "example": payload,
                    }],
                    "responses": {"200": {"description": "ok"}},
                }}}),
            ),
            (
                "header",
                _document(paths={"/a": {"get": {"responses": {"200": {
                    "description": "ok",
                    "headers": {"X-Result": {
                        "schema": {"type": "string"},
                        "example": payload,
                    }},
                }}}}}),
            ),
        )

        for label, document in cases:
            with self.subTest(label=label):
                _canonical(document)

    def test_reference_validation_matches_annotation_stripping(self):
        payload = {"$ref": "literal-annotation-data"}
        for key in ("description", "summary", "externalDocs", "example", "examples"):
            with self.subTest(key=key):
                canonical = json.loads(_canonical(_schema(**{key: payload})))
                self.assertNotIn(key, canonical["components"]["schemas"]["S"])

    def test_value_is_not_opaque_outside_an_example_object(self):
        self.assertFalse(
            _distinguishes(
                lambda value: _document(
                    components={"responses": {"R": {
                        "description": "response docs",
                        "value": {"description": value},
                    }}}
                )
            )
        )

    def test_schema_data_is_preserved_at_each_supported_grammar_position(self):
        payload = {"description": "contract data"}
        cases = (
            (
                "nested component schema",
                _schema(allOf=[{"properties": {"p": {"default": payload}}}]),
                ("components", "schemas", "S", "allOf", 0, "properties", "p"),
            ),
            (
                "response media type",
                _document(paths={"/a": {"get": {"responses": {"200": {
                    "description": "ok",
                    "content": {"application/json": {
                        "schema": {"default": payload}
                    }},
                }}}}}),
                ("paths", "/a", "get", "responses", "200", "content",
                 "application/json", "schema"),
            ),
            (
                "operation parameter",
                _document(paths={"/a": {"get": {
                    "parameters": [{
                        "name": "q", "in": "query",
                        "schema": {"default": payload},
                    }],
                    "responses": {"200": {"description": "ok"}},
                }}}),
                ("paths", "/a", "get", "parameters", 0, "schema"),
            ),
            (
                "response header",
                _document(paths={"/a": {"get": {"responses": {"200": {
                    "description": "ok",
                    "headers": {"X-Mode": {
                        "schema": {"default": payload}
                    }},
                }}}}}),
                ("paths", "/a", "get", "responses", "200", "headers",
                 "X-Mode", "schema"),
            ),
        )
        for label, document, path in cases:
            with self.subTest(position=label):
                node = json.loads(_canonical(document))
                for segment in path:
                    node = node[segment]
                self.assertEqual(node["default"], payload)


class SecurityRequirementTests(unittest.TestCase):
    """OBL-PROVIDERS-004: preserve a position, not a spelling."""

    def test_a_top_level_scheme_name_is_preserved(self):
        self.assertTrue(
            _distinguishes(lambda value: _document(security=[{value: ["read"]}]))
        )

    def test_an_operation_level_scheme_name_is_preserved(self):
        def build(value):
            return _document(paths={"/a": {"get": {
                "security": [{value: []}],
                "responses": {"200": {"description": "ok"}},
            }}})

        self.assertTrue(_distinguishes(build))

    def test_a_callback_operation_scheme_name_is_preserved(self):
        def build(value):
            return _document(paths={"/a": {"post": {
                "callbacks": {"done": {"{$request.body#/url}": {"post": {
                    "security": [{value: []}],
                    "responses": {"200": {"description": "ok"}},
                }}}},
                "responses": {"202": {"description": "accepted"}},
            }}})

        self.assertTrue(_distinguishes(build))

    def test_an_array_that_merely_sits_under_that_name_is_not_one(self):
        self.assertFalse(
            _distinguishes(
                lambda value: _schema(security=[{"description": value, "s": []}])
            )
        )

    def test_the_rule_is_keyed_on_document_position_not_name_alone(self):
        for key in ("security", "notsecurity", "securityX"):
            with self.subTest(key=key):
                kept = _distinguishes(
                    lambda value, key=key: _schema(**{
                        key: [{"description": value, "s": []}]
                    })
                )
                self.assertFalse(kept)

    def test_an_http_verb_spelling_inside_a_schema_does_not_make_an_operation(self):
        self.assertFalse(
            _distinguishes(
                lambda value: _schema(get={
                    "security": [{"description": value, "s": []}]
                })
            )
        )


class NamedMapTests(unittest.TestCase):
    """Names which resemble annotations remain valid map identifiers."""

    def test_components_examples_map_is_not_removed_as_an_annotation(self):
        self.assertTrue(
            _distinguishes(
                lambda value: _document(components={
                    "examples": {"description": {"value": value}}
                })
            )
        )

    def test_swagger_reusable_map_names_are_not_annotations(self):
        for map_name in ("responses", "securityDefinitions"):
            for entry_name in ("description", "summary", "example", "examples"):
                with self.subTest(map=map_name, entry=entry_name):
                    document = {
                        "swagger": "2.0",
                        "info": {"title": "t", "version": "1"},
                        "paths": {},
                        map_name: {entry_name: {"x-contract": "a"}},
                    }
                    canonical = json.loads(_canonical(document))
                    self.assertEqual(
                        canonical[map_name][entry_name],
                        {"x-contract": "a"},
                    )
                    changed = json.loads(json.dumps(document))
                    changed[map_name][entry_name]["x-contract"] = "b"
                    self.assertNotEqual(_canonical(document), _canonical(changed))


class CanonicalRoundTripTests(unittest.TestCase):
    """OBL-PROVIDERS-005: canonical bytes are already canonical."""

    TABLE = {
        "plain": _document(),
        "enum data": _schema(enum=[{"description": "a"}]),
        "const data": _schema(const={"description": "a"}),
        "security": _document(security=[{"anythingGoes": ["read"]}]),
        "extension": _document(**{"x-policy": {"security": [{"s": []}]}}),
        "unicode": _schema(title="é一\U0001f600"),
    }

    def test_every_table_entry_re_canonicalizes_to_itself(self):
        for label, document in self.TABLE.items():
            with self.subTest(case=label):
                once = _canonical(document)
                self.assertEqual(_canonical(json.loads(once)), once)

    @PROFILE
    @given(
        payload=st.recursive(
            st.one_of(
                st.none(),
                st.booleans(),
                st.integers(min_value=-1000, max_value=1000),
                st.text(alphabet="ab é一", max_size=4),
            ),
            lambda children: st.one_of(
                st.lists(children, max_size=3),
                st.dictionaries(
                    st.sampled_from(
                        ["description", "title", "type", "enum", "const",
                         "default", "security", "x-note", "a"]
                    ),
                    children,
                    max_size=3,
                ),
            ),
            max_leaves=8,
        )
    )
    def test_a_generated_document_re_canonicalizes_to_itself(self, payload):
        document = _schema(**{"a": payload})
        once = _canonical(document)
        self.assertEqual(_canonical(json.loads(once)), once)


if __name__ == "__main__":
    unittest.main()
