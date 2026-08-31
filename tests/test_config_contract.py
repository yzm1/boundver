"""Keep dependency-free field validation aligned with the public schema."""

import json
import unittest
from pathlib import Path

import boundver._config as config_module
import jsonschema
from boundver._config_contract import (
    BEHAVIOR_FIELDS,
    BOUNDARY_FIELDS,
    COMPONENT_FIELDS,
    COMPONENT_IDENTIFIER_PATTERN,
    DEFAULT_FIELDS,
    GIT_TAG_PREFIX_PATTERN,
    MAX_CONSUMER_GRAPH_ITEMS,
    MAX_CONSUMER_IDENTIFIER_CHARS,
    MAX_GIT_TAG_PREFIX_CHARS,
    PROVIDER_FIELDS,
    ROOT_FIELDS,
    SLICE_FIELDS,
    VERSION_FILE_FIELDS,
    VERSION_TAG_FIELDS,
)
from boundver._utils import ConfigError


class ConfigContractParityTests(unittest.TestCase):
    def test_config_error_compatibility_export_is_explicit(self) -> None:
        self.assertIs(config_module.ConfigError, ConfigError)

    def test_manual_field_contract_matches_schema(self) -> None:
        root = Path(__file__).parents[1]
        schema = json.loads(
            (root / "boundary.config.schema.json").read_text(encoding="utf-8")
        )
        properties = schema["properties"]
        component = properties["components"]["additionalProperties"]

        self.assertEqual(
            schema["$defs"]["consumerGraphIdentifier"]["maxLength"],
            MAX_CONSUMER_IDENTIFIER_CHARS,
        )
        self.assertEqual(
            schema["$defs"]["componentIdentifier"]["maxLength"],
            MAX_CONSUMER_IDENTIFIER_CHARS,
        )
        self.assertEqual(
            schema["$defs"]["componentIdentifier"]["pattern"],
            COMPONENT_IDENTIFIER_PATTERN,
        )
        self.assertEqual(
            properties["components"]["propertyNames"],
            {"$ref": "#/$defs/componentIdentifier"},
        )
        self.assertEqual(
            properties["components"]["maxProperties"],
            MAX_CONSUMER_GRAPH_ITEMS,
        )
        tag_prefix = schema["$defs"]["gitTagPrefix"]
        self.assertEqual(tag_prefix["maxLength"], MAX_GIT_TAG_PREFIX_CHARS)
        self.assertEqual(tag_prefix["pattern"], GIT_TAG_PREFIX_PATTERN)
        for field in ("consumers", "external_consumers"):
            self.assertEqual(
                component["properties"][field]["maxItems"],
                MAX_CONSUMER_GRAPH_ITEMS,
            )
        self.assertEqual(
            component["properties"]["consumers"]["items"],
            {"$ref": "#/$defs/componentIdentifier"},
        )
        self.assertEqual(
            component["properties"]["external_consumers"]["items"],
            {"$ref": "#/$defs/consumerGraphIdentifier"},
        )

        self.assertEqual(set(properties), set(ROOT_FIELDS))
        self.assertEqual(
            set(properties["defaults"]["properties"]), set(DEFAULT_FIELDS)
        )
        self.assertEqual(
            set(properties["providers"]["items"]["properties"]),
            set(PROVIDER_FIELDS),
        )
        self.assertEqual(set(component["properties"]), set(COMPONENT_FIELDS))
        self.assertEqual(
            set(component["properties"]["boundary"]["properties"]),
            set(BOUNDARY_FIELDS),
        )
        self.assertEqual(
            set(component["properties"]["behavior"]["properties"]),
            set(BEHAVIOR_FIELDS),
        )

        version_cases = component["properties"]["version_source"]["oneOf"]
        object_cases = [case for case in version_cases if case.get("type") == "object"]
        self.assertEqual(
            {frozenset(case["properties"]) for case in object_cases},
            {VERSION_FILE_FIELDS, VERSION_TAG_FIELDS},
        )
        self.assertEqual(
            set(properties["slices"]["additionalProperties"]["properties"]),
            set(SLICE_FIELDS),
        )
        slice_properties = properties["slices"]["additionalProperties"][
            "properties"
        ]
        self.assertEqual(
            slice_properties["components"]["items"],
            {"$ref": "#/$defs/componentIdentifier"},
        )
        self.assertEqual(
            slice_properties["closure_of"]["$ref"],
            "#/$defs/componentIdentifier",
        )

    def test_packaged_schema_is_byte_identical(self) -> None:
        root = Path(__file__).parents[1]
        self.assertEqual(
            (root / "boundary.config.schema.json").read_bytes(),
            (root / "src" / "boundver" / "boundary.config.schema.json").read_bytes(),
        )

    def test_schema_rejects_unaddressable_component_identifiers(self) -> None:
        root = Path(__file__).parents[1]
        schema = json.loads(
            (root / "boundary.config.schema.json").read_text(encoding="utf-8")
        )
        validator = jsonschema.Draft202012Validator(schema)

        for invalid_name in (
            "svc,prod",
            " svc",
            "svc ",
            "\u2003svc",
            "service\napi ",
        ):
            with self.subTest(name=invalid_name):
                config = {
                    "project": "p",
                    "components": {
                        invalid_name: {
                            "path": "svc",
                            "boundary": {"provider": "implicit"},
                        }
                    },
                }
                self.assertTrue(list(validator.iter_errors(config)))

        valid = {
            "project": "p",
            "components": {
                "service api": {
                    "path": "svc",
                    "boundary": {"provider": "implicit"},
                    "external_consumers": ["outside, team"],
                }
            },
            "slices": {
                "all": {"components": ["service api"]},
            },
        }
        self.assertEqual(list(validator.iter_errors(valid)), [])

        valid["components"]["service api"]["consumers"] = ["target,prod"]
        self.assertTrue(list(validator.iter_errors(valid)))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
