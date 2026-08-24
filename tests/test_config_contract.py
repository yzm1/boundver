"""Keep dependency-free field validation aligned with the public schema."""

import json
import unittest
from pathlib import Path

import boundver._config as config_module
from boundver._config_contract import (
    BEHAVIOR_FIELDS,
    BOUNDARY_FIELDS,
    COMPONENT_FIELDS,
    DEFAULT_FIELDS,
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

    def test_packaged_schema_is_byte_identical(self) -> None:
        root = Path(__file__).parents[1]
        self.assertEqual(
            (root / "boundary.config.schema.json").read_bytes(),
            (root / "src" / "boundver" / "boundary.config.schema.json").read_bytes(),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
