"""Every declared ceiling, pinned to the value it was reviewed with.

A limit is usually asserted by building an input from it: `range(LIMIT + 1)`
must be refused, `range(LIMIT)` must not. That is the right way to test a
boundary, and it leaves the boundary's own value unasserted, because every
such test follows the constant wherever it moves.

That was measured rather than assumed. Nudging by one each of the 54 ceilings
whose value is a plain integer literal, and running the tests that name it,
killed 22 and left 32: 25 that no naming test noticed, and 7 that no test
mentions at all. The other 49 ceilings here are written as expressions or as
aliases of another constant, so that sweep could not reach them and nothing is
known about them; they are pinned on the same terms.

So the numbers live here as literals. Changing one is a deliberate act: update
this table in the same commit and say in the message why the new bound is safe.
These are contract guardrails on untrusted input, not tuning knobs - a silent
tenfold rise in a step budget is a silent change to what boundver accepts.

The completeness test is the part that closes under addition: a ceiling added
later with no row here fails, so the table cannot quietly fall behind the code.

Covers OBL-GIT-SOURCE-055 and OBL-GLOBS-020, and pins the shipped value of
every other MAX_ constant the package declares.
"""

from __future__ import annotations

import ast
import importlib
import io
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "boundver"

#: (module, constant name, reviewed value). One row per declared ceiling.
DECLARED_CEILINGS = (
    ("boundver._baseline", "MAX_BASELINE_BYTES", 2_097_152),
    ("boundver._baseline", "MAX_BASELINE_TEXT", 4_096),
    ("boundver._baseline", "MAX_BASELINE_VIOLATIONS", 10_000),
    ("boundver._canonical_providers", "MAX_CANONICAL_JSON_BYTES", 52_428_800),
    ("boundver._config", "MAX_COMPONENT_EXPANSION_FILES", 50_000),
    ("boundver._config", "MAX_CONFIG_BYTES", 10_485_760),
    ("boundver._config", "MAX_DISCOVERED_COMPONENTS", 1_000),
    ("boundver._config", "MAX_DISCOVERY_MANIFESTS", 50_000),
    ("boundver._config", "MAX_FILESYSTEM_TRAVERSAL_ENTRIES", 200_000),
    ("boundver._config", "MAX_PROVIDER_DETECTION_ENTRIES", 50_000),
    ("boundver._config_contract", "MAX_CONSUMER_GRAPH_ITEMS", 10_000),
    ("boundver._config_contract", "MAX_CONSUMER_IDENTIFIER_CHARS", 16_384),
    ("boundver._config_contract", "MAX_GIT_TAG_PREFIX_CHARS", 4_096),
    ("boundver._config_contract", "MAX_COVERAGE_EXCLUSIONS", 1_000),
    ("boundver._config_contract", "MAX_COVERAGE_PATHS_PER_EXCLUSION", 256),
    ("boundver._config_contract", "MAX_COVERAGE_REASON_CHARS", 4_096),
    ("boundver._config_contract", "MAX_COVERAGE_SOURCE_INDICATORS", 256),
    ("boundver._config_contract", "MAX_DERIVATIONS", 256),
    ("boundver._config_contract", "MAX_DERIVATION_GENERATOR_CHARS", 4_096),
    ("boundver._config_contract", "MAX_DERIVATION_SELECTORS", 256),
    ("boundver._config_contract", "MAX_VERSION_CONSTANT_CHARS", 4_096),
    ("boundver._coverage", "MAX_COVERAGE_LISTED_PATHS", 1_000),
    ("boundver._coverage", "MAX_COVERAGE_PATH_CHARS", 32_768),
    ("boundver._derivations", "MAX_DERIVATION_EVIDENCE_BYTES", 65_536),
    ("boundver._derivations", "MAX_DERIVATION_EVIDENCE_FILES", 50_000),
    ("boundver._derivations", "MAX_DERIVATION_HASH_FILE_VISITS", 100_000),
    ("boundver._derivations", "MAX_DERIVATION_HASH_TOTAL_BYTES", 536_870_912),
    ("boundver._discovery", "MAX_DISCOVERY_DIFF_COMPONENTS", 10_000),
    ("boundver._discovery", "MAX_DISCOVERY_DIFF_TEXT", 16_384),
    ("boundver._discovery", "MAX_DISCOVERY_EXCLUSIONS", 1_000),
    ("boundver._discovery", "MAX_DISCOVERY_EXCLUSION_BYTES", 1_048_576),
    ("boundver._git", "MAX_FALLBACK_FILES", 50_000),
    ("boundver._git", "MAX_FALLBACK_TRAVERSAL_ENTRIES", 200_000),
    ("boundver._git", "MAX_GITIGNORE_BYTES", 1_048_576),
    ("boundver._git", "MAX_GITIGNORE_MATCH_STEPS", 10_000_000),
    ("boundver._git", "MAX_GITIGNORE_PATTERN_BYTES", 16_384),
    ("boundver._git", "MAX_GITIGNORE_RULES", 10_000),
    ("boundver._git", "MAX_GIT_BATCH_BYTES", 268_435_456),
    ("boundver._git", "MAX_GIT_BATCH_HEADER_BYTES", 65_536),
    ("boundver._git", "MAX_GIT_BLOB_BYTES", 52_428_800),
    ("boundver._git", "MAX_GIT_COMMAND_OUTPUT_BYTES", 1_048_576),
    ("boundver._git", "MAX_GIT_COMMAND_SECONDS", 300),
    ("boundver._git", "MAX_GIT_CONFIG_QUERY_SECONDS", 10),
    ("boundver._git", "MAX_GIT_DIAGNOSTIC_BYTES", 65_536),
    ("boundver._git", "MAX_GIT_FAILURE_DETAIL_CHARS", 4_096),
    ("boundver._git", "MAX_GIT_FILTER_CONFIG_KEYS", 256),
    ("boundver._git", "MAX_GIT_FILTER_DRIVERS", 64),
    ("boundver._git", "MAX_GIT_FILTER_KEY_BYTES", 512),
    ("boundver._git", "MAX_GIT_FILTER_OVERRIDE_BYTES", 16_384),
    ("boundver._git", "MAX_GIT_LIST_OUTPUT_BYTES", 33_554_432),
    ("boundver._git", "MAX_GIT_LIST_RECORD_BYTES", 16_896),
    ("boundver._git", "MAX_GIT_PATH_BYTES", 16_384),
    ("boundver._git", "MAX_PARTIAL_CLONE_CONFIG_KEYS", 64),
    ("boundver._git", "MAX_GIT_REPOSITORY_SCAN_BYTES", 17_179_869_184),
    ("boundver._git", "MAX_GIT_STATUS_FIELDS", 150_000),
    ("boundver._git", "MAX_GIT_STATUS_PATHS", 50_000),
    ("boundver._git", "MAX_GIT_TOTAL_PATH_BYTES", 16_777_216),
    ("boundver._git", "MAX_GIT_TREE_ENTRIES", 50_000),
    ("boundver._hashing", "MAX_HASH_FILES", 50_000),
    ("boundver._hashing", "MAX_HASH_FILE_BYTES", 52_428_800),
    ("boundver._hashing", "MAX_HASH_LABEL_BYTES", 16_384),
    ("boundver._hashing", "MAX_HASH_TOTAL_BYTES", 268_435_456),
    ("boundver._hashing", "MAX_HASH_TOTAL_LABEL_BYTES", 16_777_216),
    ("boundver._lockfile", "MAX_LOCKFILE_BYTES", 10_485_760),
    ("boundver._migration_analysis", "MAX_ANALYSIS_LABEL_CHARS", 4_096),
    ("boundver._migration_analysis", "MAX_ANALYSIS_SELECTOR_CHARS", 16_384),
    ("boundver._migration_analysis", "MAX_ANALYZED_DECLARATIONS", 2_000),
    ("boundver._migration_analysis", "MAX_SELECTOR_CHANGE_EXAMPLES", 5),
    ("boundver._migration_analysis", "MAX_SELECTOR_EXAMPLE_CHARS", 1_024),
    ("boundver._migration_analysis", "MAX_SELECTOR_MATCH_EVALUATIONS", 5_000_000),
    ("boundver._provider_diff", "MAX_PROVIDER_DIFF_DEPTH", 64),
    ("boundver._provider_diff", "MAX_PROVIDER_DIFF_INPUT_BYTES", 33_554_432),
    ("boundver._provider_diff", "MAX_PROVIDER_DIFF_PATH_BYTES", 16_384),
    ("boundver._provider_diff", "MAX_PROVIDER_DIFF_RESULT_BYTES", 16_777_216),
    ("boundver._provider_diff", "MAX_PROVIDER_DIFF_ROWS", 20_000),
    ("boundver._provider_diff", "MAX_PROVIDER_DIFF_WORK_STEPS", 250_000),
    ("boundver._review", "MAX_REVIEW_RESULT_BYTES", 67_108_864),
    ("boundver._review", "MAX_REVIEW_RECONCILIATION_CANDIDATES", 8),
    ("boundver._review", "MAX_REVIEW_RESULT_ROWS", 100_000),
    ("boundver._review", "MAX_REVIEW_WORK_STEPS", 250_000),
    ("boundver._review_plan", "MAX_PLAN_RESULT_BYTES", 67_108_864),
    ("boundver._review_plan", "MAX_PLAN_SUMMARY_BYTES", 65_536),
    ("boundver._review_plan", "MAX_PLAN_SUMMARY_FIELD_BYTES", 512),
    ("boundver._review_plan", "MAX_PLAN_SUMMARY_ROWS", 50),
    ("boundver._utils", "MAX_DECLARED_PATH_BYTES", 16_384),
    ("boundver._utils", "MAX_DIAGNOSTIC_BYTES", 262_144),
    ("boundver._utils", "MAX_DIAGNOSTIC_ITEMS", 256),
    ("boundver._utils", "MAX_DIAGNOSTIC_ITEM_BYTES", 8_192),
    ("boundver._utils", "MAX_DIAGNOSTIC_ITEM_CHARS", 4_096),
    ("boundver._utils", "MAX_DIAGNOSTIC_VALUE_CHARS", 500),
    ("boundver._utils", "MAX_GLOB_MATCH_STEPS", 100_000),
    ("boundver._utils", "MAX_GLOB_METACHARACTERS_PER_SEGMENT", 256),
    ("boundver._utils", "MAX_GLOB_OPERATION_STEPS", 10_000_000),
    ("boundver._utils", "MAX_GLOB_PATH_BYTES", 65_536),
    ("boundver._utils", "MAX_GLOB_PATTERN_SEGMENT_BYTES", 4_096),
    ("boundver._utils", "MAX_GLOB_SEGMENTS", 1_024),
    ("boundver._utils", "MAX_JSON_DIAGNOSTIC_PATH_BYTES", 4_096),
    ("boundver._utils", "MAX_JSON_INTEGER_DIGITS", 4_300),
    ("boundver._utils", "MAX_JSON_NUMBER_CHARACTERS", 4_332),
    ("boundver._utils", "MAX_JSON_TREE_DEPTH", 128),
    ("boundver._utils", "MAX_JSON_TREE_ISSUES", 100),
    ("boundver._utils", "MAX_JSON_TREE_NODES", 100_000),
    ("boundver._utils", "MAX_TOML_INTEGER_DIGITS", 640),
    ("boundver._utils", "MAX_YAML_COMPOSE_DEPTH", 130),
    ("boundver._utils", "MAX_YAML_COMPOSE_NODES", 200_000),
    ("boundver._utils", "MAX_YAML_INTEGER_CHARACTERS", 4_301),
    ("boundver.providers", "MAX_CUSTOM_PROVIDERS", 100),
    ("boundver.providers", "MAX_PROVIDER_DECLARATIONS", 50_000),
    ("boundver.providers", "MAX_PROVIDER_ENTRIES", 50_000),
    ("boundver.providers", "MAX_PROVIDER_ENTRY_BYTES", 52_428_800),
    ("boundver.providers", "MAX_PROVIDER_ERRORS", 100),
    ("boundver.providers", "MAX_PROVIDER_ERROR_BYTES", 16_384),
    ("boundver.providers", "MAX_PROVIDER_LABEL_BYTES", 16_384),
    ("boundver.providers", "MAX_PROVIDER_METADATA_BYTES", 1_048_576),
    ("boundver.providers", "MAX_PROVIDER_METADATA_DEPTH", 64),
    ("boundver.providers", "MAX_PROVIDER_METADATA_NODES", 100_000),
    ("boundver.providers", "MAX_PROVIDER_TOTAL_BYTES", 268_435_456),
    ("boundver.providers", "MAX_PROVIDER_TOTAL_LABEL_BYTES", 16_777_216),
    ("boundver.versions", "MAX_VERSION_FILE_BYTES", 10_485_760),
)


def _declared_ceiling_names():
    """Every module-level MAX_ integer the package declares, by module."""
    found = set()
    for path in sorted(SRC.glob("*.py")):
        if path.name == "__init__.py":
            continue
        try:
            tree = ast.parse(io.open(path, encoding="utf-8").read())
        except SyntaxError:  # pragma: no cover - the package must parse
            continue
        module = "boundver." + path.stem
        imported = importlib.import_module(module)
        for node in tree.body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name):
                continue
            if not target.id.startswith("MAX_"):
                continue
            value = getattr(imported, target.id, None)
            if isinstance(value, int) and not isinstance(value, bool):
                found.add((module, target.id))
    return found


class DeclaredCeilingTests(unittest.TestCase):
    """The shipped bounds, and the table that has to keep up with them."""

    def test_each_ceiling_still_has_the_value_it_was_reviewed_with(self):
        for module, name, reviewed in DECLARED_CEILINGS:
            with self.subTest(ceiling=name):
                imported = importlib.import_module(module)
                self.assertEqual(getattr(imported, name), reviewed)

    def test_the_table_covers_every_ceiling_the_package_declares(self):
        """A new guardrail cannot be added without a reviewed value."""
        self.assertEqual(
            _declared_ceiling_names(),
            {(module, name) for module, name, _ in DECLARED_CEILINGS},
        )

    def test_the_table_names_no_ceiling_twice(self):
        """The premise: a duplicated row could hide a stale one."""
        names = [(module, name) for module, name, _ in DECLARED_CEILINGS]
        self.assertEqual(len(names), len(set(names)))

    def test_every_ceiling_is_a_positive_integer(self):
        """A bound of zero or below would refuse everything, silently."""
        for module, name, reviewed in DECLARED_CEILINGS:
            with self.subTest(ceiling=name):
                self.assertIsInstance(reviewed, int)
                self.assertGreater(reviewed, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
