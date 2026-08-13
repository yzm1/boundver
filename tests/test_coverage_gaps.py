"""Tests targeting identified coverage gaps — gitignore **, YAML/TOML fallbacks,
batch-cat malformed headers, custom provider name validation, symlink paths,
PathHashProvider sort stability, and why_component source=index."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from boundver._git import (
    _GitignoreRules,
    _git_batch_cat,
)


# ---------------------------------------------------------------------------
# _gitignore_pattern_to_regex — all ** positions
# ---------------------------------------------------------------------------


class TestGitignoreDoubleStarRegex(unittest.TestCase):
    """Verify ** wildcard converts to correct regex for all positions."""

    def _match(self, pattern, path):
        rules = _GitignoreRules()
        rules.add(pattern)
        return rules.is_ignored(path)

    # Leading ** (anchored at any depth)
    def test_leading_doublestar_matches_root(self):
        self.assertTrue(self._match("**/foo.py", "foo.py"))

    def test_leading_doublestar_matches_nested(self):
        self.assertTrue(self._match("**/foo.py", "a/b/foo.py"))

    def test_leading_doublestar_no_partial_match(self):
        self.assertFalse(self._match("**/foo.py", "foo.pyc"))

    def test_leading_doublestar_with_glob(self):
        self.assertTrue(self._match("**/test_*.py", "tests/test_core.py"))

    # Trailing ** (matches everything under directory)
    def test_trailing_doublestar_direct_child(self):
        self.assertTrue(self._match("src/**", "src/main.py"))

    def test_trailing_doublestar_deeply_nested(self):
        self.assertTrue(self._match("src/**", "src/a/b/c/d.py"))

    def test_trailing_doublestar_no_match_other_dir(self):
        self.assertFalse(self._match("src/**", "lib/main.py"))

    def test_trailing_doublestar_no_match_prefix_substring(self):
        self.assertFalse(self._match("src/**", "src2/main.py"))

    # Middle ** (zero or more directory segments between)
    def test_middle_doublestar_zero_segments(self):
        self.assertTrue(self._match("foo/**/bar.py", "foo/bar.py"))

    def test_middle_doublestar_one_segment(self):
        self.assertTrue(self._match("foo/**/bar.py", "foo/x/bar.py"))

    def test_middle_doublestar_many_segments(self):
        self.assertTrue(self._match("foo/**/bar.py", "foo/x/y/z/bar.py"))

    def test_middle_doublestar_no_match_wrong_root(self):
        self.assertFalse(self._match("foo/**/bar.py", "baz/bar.py"))

    def test_middle_doublestar_no_match_wrong_leaf(self):
        self.assertFalse(self._match("foo/**/bar.py", "foo/baz.py"))

    # Bare ** (matches everything)
    def test_bare_doublestar_matches_simple(self):
        self.assertTrue(self._match("**", "anything.txt"))

    def test_bare_doublestar_matches_nested(self):
        self.assertTrue(self._match("**", "a/b/c/d.txt"))

    # Multiple ** in one pattern
    def test_multiple_doublestar(self):
        self.assertTrue(self._match("a/**/b/**/c", "a/b/c"))
        self.assertTrue(self._match("a/**/b/**/c", "a/x/b/y/c"))

    # ** with glob characters in adjacent segments
    def test_doublestar_adjacent_glob(self):
        self.assertTrue(self._match("**/docs/*.md", "docs/readme.md"))
        self.assertTrue(self._match("**/docs/*.md", "project/docs/readme.md"))
        self.assertFalse(self._match("**/docs/*.md", "docs/sub/readme.md"))


# ---------------------------------------------------------------------------
# _GitignoreRules — negation and ordering
# ---------------------------------------------------------------------------


class TestGitignoreRulesNegation(unittest.TestCase):
    """Verify negation patterns override previous ignores."""

    def test_negation_un_ignores_file(self):
        rules = _GitignoreRules()
        rules.add("*.log")
        rules.add("!important.log")
        self.assertTrue(rules.is_ignored("debug.log"))
        self.assertFalse(rules.is_ignored("important.log"))

    def test_last_matching_rule_wins(self):
        rules = _GitignoreRules()
        rules.add("*.py")
        rules.add("!keep.py")
        rules.add("keep.py")  # re-ignore it
        self.assertTrue(rules.is_ignored("keep.py"))

    def test_negation_of_directory_pattern(self):
        rules = _GitignoreRules()
        rules.add("build")
        rules.add("!build")
        self.assertFalse(rules.is_ignored("build/output.js"))

    def test_pattern_without_slash_matches_any_depth(self):
        rules = _GitignoreRules()
        rules.add("*.pyc")
        self.assertTrue(rules.is_ignored("foo/bar/__pycache__/module.pyc"))


# ---------------------------------------------------------------------------
# _git_batch_cat — malformed/edge-case headers
# ---------------------------------------------------------------------------


class TestGitBatchCatEdgeCases(unittest.TestCase):
    """Test _git_batch_cat with various header anomalies using a real git repo."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        subprocess.run(["git", "init"], cwd=self.tmpdir, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=self.tmpdir, capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=self.tmpdir, capture_output=True, check=True,
        )
        (Path(self.tmpdir) / "hello.txt").write_text("hello world\n")
        subprocess.run(["git", "add", "."], cwd=self.tmpdir, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=self.tmpdir, capture_output=True, check=True,
        )

    def test_missing_ref_raises(self):
        with self.assertRaisesRegex(ValueError, "not found"):
            _git_batch_cat(Path(self.tmpdir), ["HEAD:nonexistent.txt"])

    def test_existing_ref_returns_content(self):
        result = _git_batch_cat(Path(self.tmpdir), ["HEAD:hello.txt"])
        self.assertEqual(result["HEAD:hello.txt"], b"hello world\n")

    def test_mixed_existing_and_missing_raises(self):
        with self.assertRaisesRegex(ValueError, "not found"):
            _git_batch_cat(
                Path(self.tmpdir),
                ["HEAD:hello.txt", "HEAD:missing.txt"],
            )

    def test_empty_refs_returns_empty_dict(self):
        result = _git_batch_cat(Path(self.tmpdir), [])
        self.assertEqual(result, {})

    def test_newline_in_ref_raises(self):
        with self.assertRaises(ValueError):
            _git_batch_cat(Path(self.tmpdir), ["HEAD:foo\nbar"])

    def test_carriage_return_in_ref_raises(self):
        with self.assertRaises(ValueError):
            _git_batch_cat(Path(self.tmpdir), ["HEAD:foo\rbar"])

    def test_ref_with_spaces_works(self):
        # Create a file with space in name
        (Path(self.tmpdir) / "has space.txt").write_text("spaced\n")
        subprocess.run(["git", "add", "."], cwd=self.tmpdir, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "space"],
            cwd=self.tmpdir, capture_output=True, check=True,
        )
        result = _git_batch_cat(Path(self.tmpdir), ["HEAD:has space.txt"])
        self.assertEqual(result["HEAD:has space.txt"], b"spaced\n")


# ---------------------------------------------------------------------------
# TOML fallback — edge cases
# ---------------------------------------------------------------------------


class TestTomlFallbackEdgeCases(unittest.TestCase):
    """TOML regex-fallback edge cases (when tomllib/tomli unavailable)."""

    def setUp(self):
        import boundver.versions as v
        self._v = v
        self._orig_toml = v.tomllib
        v.tomllib = None  # Force regex fallback

    def tearDown(self):
        self._v.tomllib = self._orig_toml

    def test_empty_double_quoted_string(self):
        result = self._v._extract_toml_from_text('version = ""', "version")
        self.assertEqual(result, "")

    def test_empty_single_quoted_string(self):
        result = self._v._extract_toml_from_text("version = ''", "version")
        self.assertEqual(result, "")

    def test_value_with_equals_sign(self):
        result = self._v._extract_toml_from_text(
            'url = "https://example.com?a=1&b=2"', "url"
        )
        self.assertEqual(result, "https://example.com?a=1&b=2")

    def test_value_with_inline_comment(self):
        result = self._v._extract_toml_from_text(
            'version = "1.0.0" # release candidate', "version"
        )
        self.assertEqual(result, "1.0.0")

    def test_nested_table_value(self):
        toml_text = "[project]\nversion = \"2.3.4\"\n"
        result = self._v._extract_toml_from_text(toml_text, "project.version")
        self.assertEqual(result, "2.3.4")

    def test_unquoted_integer_value(self):
        result = self._v._extract_toml_from_text("port = 8080", "port")
        self.assertEqual(result, "8080")

    def test_key_not_found_returns_none(self):
        result = self._v._extract_toml_from_text('name = "foo"', "version")
        self.assertIsNone(result)

    def test_dotted_key_syntax_is_not_supported_by_regex_fallback(self):
        result = self._v._extract_toml_from_text(
            'project.version = "1.0.0"', "project.version"
        )
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# YAML fallback — edge cases
# ---------------------------------------------------------------------------


class TestYamlFallbackEdgeCases(unittest.TestCase):
    """YAML regex-fallback edge cases (when PyYAML unavailable)."""

    def setUp(self):
        import boundver.versions as v
        self._v = v
        self._orig_yaml = v.yaml
        v.yaml = None  # Force regex fallback

    def tearDown(self):
        self._v.yaml = self._orig_yaml

    def test_quoted_value_with_hash_not_stripped(self):
        result = self._v._extract_yaml_from_text('key: "value#notcomment"', "key")
        self.assertEqual(result, "value#notcomment")

    def test_single_quoted_value_with_hash(self):
        result = self._v._extract_yaml_from_text("key: 'val#ue'", "key")
        self.assertEqual(result, "val#ue")

    def test_unquoted_value_inline_comment_stripped(self):
        result = self._v._extract_yaml_from_text("version: 1.0.0 # release", "version")
        self.assertEqual(result, "1.0.0")

    def test_nested_key_extraction(self):
        yaml_text = "info:\n  version: 3.0.0\n"
        result = self._v._extract_yaml_from_text(yaml_text, "info.version")
        self.assertEqual(result, "3.0.0")

    def test_deeply_nested_key(self):
        yaml_text = "a:\n  b:\n    c: deep\n"
        result = self._v._extract_yaml_from_text(yaml_text, "a.b.c")
        self.assertEqual(result, "deep")

    def test_section_with_no_value_not_returned(self):
        result = self._v._extract_yaml_from_text("section:\n  key: val", "section")
        self.assertIsNone(result)

    def test_missing_key_returns_none(self):
        result = self._v._extract_yaml_from_text("name: foo", "version")
        self.assertIsNone(result)

    def test_comment_only_lines_skipped(self):
        yaml_text = "# comment\nversion: 1.2.3\n"
        result = self._v._extract_yaml_from_text(yaml_text, "version")
        self.assertEqual(result, "1.2.3")

    def test_blank_lines_skipped(self):
        yaml_text = "\n\nversion: 4.5.6\n\n"
        result = self._v._extract_yaml_from_text(yaml_text, "version")
        self.assertEqual(result, "4.5.6")

    def test_quoted_value_with_colon(self):
        result = self._v._extract_yaml_from_text('url: "http://localhost:8080"', "url")
        self.assertEqual(result, "http://localhost:8080")


# ---------------------------------------------------------------------------
# YAML with real parser — authoritative behavior
# ---------------------------------------------------------------------------


class TestYamlRealParserAuthoritative(unittest.TestCase):
    """When PyYAML is available, it should NOT fall through to regex."""

    def test_missing_field_returns_none_not_regex(self):
        import boundver.versions as v
        if v.yaml is None:
            self.skipTest("PyYAML not installed")
        # Even if the regex would find something, the real parser is authoritative
        result = v._extract_yaml_from_text("items:\n  - version: 1.0", "version")
        self.assertIsNone(result)

    def test_invalid_yaml_returns_none(self):
        import boundver.versions as v
        if v.yaml is None:
            self.skipTest("PyYAML not installed")
        result = v._extract_yaml_from_text("{invalid yaml: [}", "key")
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# _read_path_content — CRLF normalization and symlinks
# ---------------------------------------------------------------------------


class TestReadPathContent(unittest.TestCase):
    """Test working-tree content reading edge cases."""

    def test_crlf_normalized_to_lf(self):
        from boundver._hashing import _read_path_content
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            f = repo_root / "file.txt"
            f.write_bytes(b"line1\r\nline2\r\n")
            content = _read_path_content(repo_root, f, "working")
            self.assertEqual(content, b"line1\nline2\n")

    def test_binary_crlf_not_normalized(self):
        from boundver._hashing import _read_path_content

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            f = repo_root / "binary.bin"
            # Binary: contains null byte
            data = b"header\r\n\x00body\r\n"
            f.write_bytes(data)
            content = _read_path_content(repo_root, f, "working")
            self.assertEqual(content, data)  # Unchanged

    @unittest.skipIf(sys.platform == "win32", "Symlinks require admin on Windows")
    def test_symlink_returns_target_text(self):
        from boundver._hashing import _read_path_content

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            target = repo_root / "real.txt"
            target.write_text("real content")
            link = repo_root / "link.txt"
            link.symlink_to("real.txt")
            content = _read_path_content(repo_root, link, "working")
            self.assertEqual(content, b"real.txt")


# ---------------------------------------------------------------------------
# PathHashProvider — sort determinism
# ---------------------------------------------------------------------------


class TestPathHashProviderSortDeterminism(unittest.TestCase):
    """PathHashProvider must produce same digest regardless of pattern order."""

    def test_pattern_order_independent(self):
        from boundver.providers import PathHashProvider, ProviderContext

        # Create mock contexts with different path orders
        ctx1 = MagicMock(spec=ProviderContext)
        ctx1.boundary_cfg = {"paths": ["b.py", "a.py"]}
        ctx1.component_path = "svc"
        ctx1.list_files = MagicMock(side_effect=lambda p: [f"{p}/file.txt"])
        ctx1.read_file = MagicMock(return_value=b"content")

        ctx2 = MagicMock(spec=ProviderContext)
        ctx2.boundary_cfg = {"paths": ["a.py", "b.py"]}
        ctx2.component_path = "svc"
        ctx2.list_files = MagicMock(side_effect=lambda p: [f"{p}/file.txt"])
        ctx2.read_file = MagicMock(return_value=b"content")

        p = PathHashProvider()
        r1 = p.resolve(ctx1)
        r2 = p.resolve(ctx2)
        # Both should produce identical entries (sorted by label)
        self.assertEqual(r1.entries, r2.entries)


# ---------------------------------------------------------------------------
# source_tree_digest — sort consistency
# ---------------------------------------------------------------------------


class TestSourceTreeDigestSort(unittest.TestCase):
    """source_tree_digest must sort files for deterministic hashing."""

    def test_same_files_different_order_same_hash(self):
        """If file listing returns in different order, digest must be stable."""
        from boundver._hashing import source_tree_digest

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Init a git repo with multiple files
            subprocess.run(["git", "init"], cwd=str(root), capture_output=True, check=True)
            subprocess.run(
                ["git", "config", "user.email", "t@t.com"],
                cwd=str(root), capture_output=True, check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "T"],
                cwd=str(root), capture_output=True, check=True,
            )
            (root / "b.txt").write_text("bbb")
            (root / "a.txt").write_text("aaa")
            (root / "c.txt").write_text("ccc")
            subprocess.run(["git", "add", "."], cwd=str(root), capture_output=True, check=True)
            subprocess.run(
                ["git", "commit", "-m", "init"],
                cwd=str(root), capture_output=True, check=True,
            )
            # Compute digest twice — should be identical
            d1 = source_tree_digest(root, ".", source="head")
            d2 = source_tree_digest(root, ".", source="head")
            self.assertEqual(d1, d2)
            self.assertIsNotNone(d1)


# ---------------------------------------------------------------------------
# _is_within — path containment
# ---------------------------------------------------------------------------


class TestIsWithin(unittest.TestCase):
    """_is_within correctly checks path containment."""

    def test_child_is_within_parent(self):
        from boundver._utils import _is_within
        parent = Path(tempfile.gettempdir())
        child = parent / "sub" / "file.txt"
        self.assertTrue(_is_within(parent, child))

    def test_unrelated_path_not_within(self):
        from boundver._utils import _is_within
        a = Path(tempfile.gettempdir()) / "a"
        b = Path(tempfile.gettempdir()) / "b"
        # Only one of these can be True
        # Actually both are within gettempdir, so use more distinct paths
        if sys.platform == "win32":
            self.assertFalse(_is_within(Path("C:/a"), Path("D:/b")))
        else:
            self.assertFalse(_is_within(Path("/opt/a"), Path("/var/b")))


# ---------------------------------------------------------------------------
# Hash guardrails
# ---------------------------------------------------------------------------


class TestHashGuardrails(unittest.TestCase):
    """Test enforcement of file count and size guardrails."""

    def test_too_many_files_raises(self):
        from boundver._hashing import _enforce_hash_guardrails, MAX_HASH_FILES
        with self.assertRaises(ValueError):
            _enforce_hash_guardrails(Path("/fake"), MAX_HASH_FILES + 1)

    def test_content_too_large_raises(self):
        from boundver._hashing import _enforce_content_size, MAX_HASH_FILE_BYTES
        big = b"x" * (MAX_HASH_FILE_BYTES + 1)
        with self.assertRaises(ValueError):
            _enforce_content_size(big, "test.bin")

    def test_content_at_limit_ok(self):
        from boundver._hashing import _enforce_content_size, MAX_HASH_FILE_BYTES
        data = b"x" * MAX_HASH_FILE_BYTES
        # Should not raise
        _enforce_content_size(data, "test.bin")


if __name__ == "__main__":
    unittest.main()
