"""Tables for three contracts the suite reached but never asserted.

Each of these is a promise made in a docstring or a schema and exercised only
incidentally: a hook that must always return a list whatever the provider does,
a source-mode set that must be the only accepted spellings, and a partition
that must place every component exactly once.

Covers OBL-PROVIDERS-043, OBL-PROVIDERS-044, OBL-LOCKFILE-061, OBL-CONFIG-051
and OBL-CONFIG-052.
"""

from __future__ import annotations

import unittest

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from boundver._discovery import (
    MAX_DISCOVERY_DIFF_COMPONENTS,
    compare_discovery_to_config,
)
from boundver._lockfile import generate_lockfile
from boundver._utils import SOURCE_MODE_SET, ConfigError
from boundver.providers import validate_provider_environment

from tests._scenarios import SOURCE_MODES, Scenario

PROFILE = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.filter_too_much],
)


class _NoName:
    def validate_environment(self, boundary_cfg):
        return []


class _NonStringName:
    name = 123

    def validate_environment(self, boundary_cfg):
        return []


class _RaisingName:
    @property
    def name(self):
        raise RuntimeError("name is not readable")

    def validate_environment(self, boundary_cfg):
        return []


class _RaisingHook:
    name = "raiser"

    def validate_environment(self, boundary_cfg):
        raise RuntimeError("hook exploded")


class _NonListReturn:
    name = "bad-return"

    def validate_environment(self, boundary_cfg):
        return "not a list"


class _NoHook:
    name = "plain"


class _Reporting:
    name = "reporting"

    def validate_environment(self, boundary_cfg):
        return ["dependency missing"]


class ValidateEnvironmentTests(unittest.TestCase):
    """OBL-PROVIDERS-043 and 044: a list comes back, whatever the provider is.

    The hook runs arbitrary provider code and reads a `name` attribute that a
    custom provider controls. Its docstring promises a list of errors, and a
    caller that assumed anything else would break on the first misbehaving
    provider rather than reporting it.
    """

    MISBEHAVING = (
        ("no name attribute", _NoName),
        ("non-string name", _NonStringName),
        ("name raises on access", _RaisingName),
        ("hook raises", _RaisingHook),
        ("hook returns a non-list", _NonListReturn),
        ("no hook at all", _NoHook),
        ("hook reports an error", _Reporting),
    )

    def test_every_provider_shape_yields_a_list(self):
        for label, provider in self.MISBEHAVING:
            with self.subTest(provider=label):
                result = validate_provider_environment(provider(), {})
                self.assertIsInstance(result, list)
                for entry in result:
                    self.assertIsInstance(entry, str)

    def test_a_raising_hook_is_reported_rather_than_propagated(self):
        result = validate_provider_environment(_RaisingHook(), {})
        self.assertEqual(len(result), 1)
        self.assertIn("hook exploded", result[0])
        self.assertIn("raiser", result[0])

    def test_a_process_control_exception_is_reported_rather_than_propagated(self):
        class Exiting:
            name = "exiting"

            def validate_environment(self, boundary_cfg):
                raise SystemExit(0)

        result = validate_provider_environment(Exiting(), {})
        self.assertEqual(len(result), 1)
        self.assertIn("environment validation failed", result[0])
        self.assertIn("SystemExit", result[0])

    def test_a_non_list_return_is_refused_by_name(self):
        result = validate_provider_environment(_NonListReturn(), {})
        self.assertEqual(len(result), 1)
        self.assertIn("must return a list", result[0])

    def test_a_provider_with_no_hook_reports_nothing(self):
        self.assertEqual(validate_provider_environment(_NoHook(), {}), [])

    def test_an_unreadable_name_does_not_stop_the_hook_running(self):
        """The name is for the message; failing to read it must not lose the check."""
        self.assertEqual(validate_provider_environment(_RaisingName(), {}), [])


class SourceModeAcceptanceTests(unittest.TestCase):
    """OBL-LOCKFILE-061: the declared set is the whole set."""

    REJECTED = (
        ("uppercase", "HEAD"),
        ("wrong word", "worktree"),
        ("empty", ""),
        ("none", None),
        ("integer", 1),
        ("trailing space", "head "),
        ("mixed case", "Index"),
        ("plural", "heads"),
    )

    def _scene(self) -> Scenario:
        scene = Scenario()
        scene.component("svc", path="svc", boundary=["api"])
        scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
        scene.commit()
        return scene

    def test_the_fixture_and_the_code_agree_on_the_set(self):
        """The fixture's SOURCE_MODES is used across the suite; pin the tie."""
        self.assertEqual(set(SOURCE_MODES), set(SOURCE_MODE_SET))

    def test_every_declared_mode_is_accepted(self):
        with self._scene() as scene:
            for mode in sorted(SOURCE_MODE_SET):
                with self.subTest(mode=mode):
                    self.assertIn("components", scene.generate(source=mode))

    def test_every_other_spelling_is_refused_by_name(self):
        with self._scene() as scene:
            for label, value in self.REJECTED:
                with self.subTest(spelling=label):
                    with self.assertRaises(ConfigError) as caught:
                        generate_lockfile(scene.config, scene.root, source=value)
                    message = str(caught.exception)
                    self.assertIn("Unknown source mode", message)
                    for mode in sorted(SOURCE_MODE_SET):
                        self.assertIn(mode, message)


class DiscoveryPartitionTests(unittest.TestCase):
    """OBL-CONFIG-051 and 052: three buckets, every component in exactly one."""

    @staticmethod
    def _buckets(result: dict):
        return (
            {entry["discovered_name"] for entry in result["registered"]},
            {entry["name"] for entry in result["unregistered"]},
            {entry["name"] for entry in result["not_discovered"]},
        )

    def test_a_worked_example_partitions_as_documented(self):
        discovered = {name: {"path": name} for name in ("a", "b", "c")}
        config = {"components": {name: {"path": name} for name in ("b", "c", "d")}}
        registered, unregistered, not_discovered = self._buckets(
            compare_discovery_to_config(discovered, config)
        )
        self.assertEqual(registered, {"b", "c"})
        self.assertEqual(unregistered, {"a"})
        self.assertEqual(not_discovered, {"d"})

    def test_the_counts_match_the_rows_they_summarize(self):
        discovered = {name: {"path": name} for name in ("a", "b", "c")}
        config = {"components": {name: {"path": name} for name in ("b", "d")}}
        result = compare_discovery_to_config(discovered, config)
        for bucket in ("registered", "unregistered", "not_discovered"):
            with self.subTest(bucket=bucket):
                self.assertEqual(result[f"{bucket}_count"], len(result[bucket]))

    @PROFILE
    @given(
        discovered_names=st.sets(st.sampled_from("abcdef"), max_size=6),
        configured_names=st.sets(st.sampled_from("abcdef"), max_size=6),
    )
    def test_every_component_lands_in_exactly_one_bucket(
        self, discovered_names, configured_names
    ):
        discovered = {name: {"path": name} for name in discovered_names}
        config = {"components": {name: {"path": name} for name in configured_names}}
        registered, unregistered, not_discovered = self._buckets(
            compare_discovery_to_config(discovered, config)
        )
        self.assertFalse(registered & unregistered)
        self.assertFalse(registered & not_discovered)
        self.assertFalse(unregistered & not_discovered)
        self.assertEqual(registered | unregistered, discovered_names)
        self.assertEqual(registered | not_discovered, configured_names)

    def test_an_empty_config_is_refused_rather_than_partitioned(self):
        with self.assertRaises(ConfigError):
            compare_discovery_to_config({}, {"components": "not an object"})

    def test_the_component_ceiling_refuses_rather_than_truncating(self):
        """A truncated partition would silently under-report unregistered work."""
        limit = MAX_DISCOVERY_DIFF_COMPONENTS
        config = {"components": {f"c{i}": {"path": f"p{i}"} for i in range(limit)}}
        result = compare_discovery_to_config({}, config)
        self.assertEqual(result["not_discovered_count"], limit)

        config["components"][f"c{limit}"] = {"path": f"p{limit}"}
        with self.assertRaises(ConfigError) as caught:
            compare_discovery_to_config({}, config)
        self.assertIn(str(limit), str(caught.exception))


if __name__ == "__main__":
    unittest.main()
