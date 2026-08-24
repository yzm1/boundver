"""Compatibility contracts for names retained by ``boundver.core``.

The implementation now lives in focused private modules. These aliases remain
only because earlier callers imported them from the original monolithic module.
"""

import unittest

import boundver._config as config
import boundver._git as git_helpers
import boundver._hashing as hashing
import boundver._lockfile as lockfile
import boundver.core as core
import boundver.versions as versions


class CoreCompatibilityReexportTests(unittest.TestCase):
    def test_legacy_reexports_resolve_to_their_owning_modules(self):
        expected = {
            "_is_ignored": git_helpers._is_ignored,
            "git_latest_tag": git_helpers.git_latest_tag,
            "list_head_files": git_helpers.list_head_files,
            "_git_batch_cat": git_helpers._git_batch_cat,
            "MAX_HASH_FILES": hashing.MAX_HASH_FILES,
            "_content_only_digest": hashing._content_only_digest,
            "_enforce_content_size": hashing._enforce_content_size,
            "_enforce_hash_guardrails": hashing._enforce_hash_guardrails,
            "_read_path_content": hashing._read_path_content,
            "canonical_json": hashing.canonical_json,
            "sha256_hex": hashing.sha256_hex,
            "source_tree_digest": hashing.source_tree_digest,
            "_schema_engine_errors": config._schema_engine_errors,
            "_recompute_slice_entry": lockfile._recompute_slice_entry,
            "_extract_toml_field": versions._extract_toml_field,
            "_extract_yaml_field": versions._extract_yaml_field,
            "parse_semver": versions.parse_semver,
        }
        for name, owner in expected.items():
            with self.subTest(name=name):
                self.assertIs(getattr(core, name), owner)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
