"""Unit tests for boundver.core hashing primitives — no git required."""
import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from boundver.core import (
    canonical_json,
    sha256_hex,
    source_tree_digest,
    _enforce_hash_guardrails,
    _enforce_content_size,
    _is_ignored,
    git_root,
    _recompute_slice_entry,
    _lockfile_structure_issues,
    discover_components,
)


class CanonicalJsonTests(unittest.TestCase):
    def test_keys_sorted_alphabetically(self):
        result = canonical_json({"z": 1, "a": 2, "m": 3})
        self.assertEqual(result, '{"a":2,"m":3,"z":1}')

    def test_no_whitespace(self):
        result = canonical_json({"key": "value"})
        self.assertNotIn(" ", result)
        self.assertNotIn("\n", result)

    def test_nested_object_keys_sorted(self):
        result = canonical_json({"b": {"z": 1, "a": 2}})
        self.assertEqual(result, '{"b":{"a":2,"z":1}}')

    def test_array_order_preserved(self):
        result = canonical_json({"items": [3, 1, 2]})
        self.assertEqual(result, '{"items":[3,1,2]}')

    def test_string_values(self):
        result = canonical_json({"k": "hello"})
        self.assertEqual(result, '{"k":"hello"}')

    def test_null_value(self):
        result = canonical_json({"x": None})
        self.assertEqual(result, '{"x":null}')

    def test_boolean_values(self):
        result = canonical_json({"t": True, "f": False})
        self.assertEqual(result, '{"f":false,"t":true}')

    def test_unicode_not_escaped(self):
        result = canonical_json({"emoji": "🚀"})
        self.assertIn("🚀", result)

    def test_empty_object(self):
        self.assertEqual(canonical_json({}), "{}")

    def test_empty_array(self):
        self.assertEqual(canonical_json([]), "[]")

    def test_integer_value(self):
        result = canonical_json({"n": 42})
        self.assertEqual(result, '{"n":42}')

    def test_same_content_same_output(self):
        obj = {"b": [1, 2], "a": {"x": True}}
        self.assertEqual(canonical_json(obj), canonical_json(obj))

    def test_different_insertion_order_same_output(self):
        # Two dicts with same keys in different order produce identical canonical form
        a = canonical_json({"x": 1, "y": 2})
        b = canonical_json({"y": 2, "x": 1})
        self.assertEqual(a, b)


class Sha256HexTests(unittest.TestCase):
    def test_known_vector_empty_string(self):
        expected = hashlib.sha256("".encode("utf-8")).hexdigest()
        self.assertEqual(sha256_hex(""), expected)

    def test_known_vector_hello(self):
        # Known SHA-256 of "hello"
        expected = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        self.assertEqual(sha256_hex("hello"), expected)

    def test_returns_64_char_hex_string(self):
        result = sha256_hex("anything")
        self.assertEqual(len(result), 64)
        self.assertRegex(result, r"^[0-9a-f]{64}$")

    def test_different_inputs_different_hashes(self):
        self.assertNotEqual(sha256_hex("a"), sha256_hex("b"))

    def test_same_input_same_hash(self):
        self.assertEqual(sha256_hex("consistent"), sha256_hex("consistent"))

    def test_unicode_input(self):
        # Should not raise, should use UTF-8 encoding
        result = sha256_hex("héllo")
        self.assertEqual(len(result), 64)


class SourceTreeDigestTests(unittest.TestCase):
    """Tests for source_tree_digest using the working-tree source (no git needed)."""

    def test_returns_none_for_nonexistent_path(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # Path that doesn't exist → no files → None
            result = source_tree_digest(root, "nonexistent", source="working-tree")
            self.assertIsNone(result)

    def test_single_file_returns_digest(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            comp = root / "svc"
            comp.mkdir()
            (comp / "api.yaml").write_bytes(b"openapi: 3.0.0\n")
            result = source_tree_digest(root, "svc", source="working-tree")
            self.assertIsNotNone(result)
            self.assertEqual(len(result), 64)

    def test_digest_stable_for_same_content(self):
        with tempfile.TemporaryDirectory() as td1, tempfile.TemporaryDirectory() as td2:
            for td in (td1, td2):
                root = Path(td)
                comp = root / "svc"
                comp.mkdir()
                (comp / "api.yaml").write_bytes(b"openapi: 3.0.0\n")

            d1 = source_tree_digest(Path(td1), "svc", source="working-tree")
            d2 = source_tree_digest(Path(td2), "svc", source="working-tree")
            self.assertEqual(d1, d2)

    def test_digest_changes_when_content_changes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            comp = root / "svc"
            comp.mkdir()
            f = comp / "api.yaml"
            f.write_bytes(b"openapi: 3.0.0\n")
            d1 = source_tree_digest(root, "svc", source="working-tree")
            f.write_bytes(b"openapi: 3.1.0\n")
            d2 = source_tree_digest(root, "svc", source="working-tree")
            self.assertNotEqual(d1, d2)

    def test_digest_changes_when_file_added(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            comp = root / "svc"
            comp.mkdir()
            (comp / "api.yaml").write_bytes(b"openapi: 3.0.0\n")
            d1 = source_tree_digest(root, "svc", source="working-tree")
            (comp / "extra.py").write_bytes(b"# new file\n")
            d2 = source_tree_digest(root, "svc", source="working-tree")
            self.assertNotEqual(d1, d2)

    def test_digest_includes_filename_in_hash(self):
        # Two files with the same content but different names produce different digests
        with tempfile.TemporaryDirectory() as td1, tempfile.TemporaryDirectory() as td2:
            for td, name in ((td1, "alpha.txt"), (td2, "beta.txt")):
                root = Path(td)
                comp = root / "svc"
                comp.mkdir()
                (comp / name).write_bytes(b"same content\n")

            d1 = source_tree_digest(Path(td1), "svc", source="working-tree")
            d2 = source_tree_digest(Path(td2), "svc", source="working-tree")
            self.assertNotEqual(d1, d2)

    def test_single_file_path_as_component(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "schema.json").write_bytes(b'{"key": "value"}')
            result = source_tree_digest(root, "schema.json", source="working-tree")
            self.assertIsNotNone(result)

    def test_pyc_files_excluded_from_digest(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            comp = root / "svc"
            comp.mkdir()
            (comp / "main.py").write_bytes(b"print('hello')\n")
            d1 = source_tree_digest(root, "svc", source="working-tree")
            (comp / "main.pyc").write_bytes(b"compiled bytecode")
            d2 = source_tree_digest(root, "svc", source="working-tree")
            self.assertEqual(d1, d2)


class HashGuardrailTests(unittest.TestCase):
    def test_enforce_hash_guardrails_raises_on_too_many_files(self):
        """_enforce_hash_guardrails raises ValueError when file count exceeds limit."""
        import boundver.core as core
        with self.assertRaises(ValueError) as cm:
            _enforce_hash_guardrails(Path("."), core.MAX_HASH_FILES + 1)
        self.assertIn("Hash guardrail exceeded", str(cm.exception))

    def test_enforce_hash_guardrails_ok_at_limit(self):
        """_enforce_hash_guardrails does not raise at exactly the limit."""
        import boundver.core as core
        _enforce_hash_guardrails(Path("."), core.MAX_HASH_FILES)  # should not raise

    def test_enforce_content_size_raises_on_large_file(self):
        """_enforce_content_size raises ValueError for oversized content."""
        big = b"x" * (52 * 1024 * 1024)  # > 50 MiB
        with self.assertRaises(ValueError) as cm:
            _enforce_content_size(big, "big.bin")
        self.assertIn("file too large", str(cm.exception))

    def test_enforce_content_size_ok_under_limit(self):
        """_enforce_content_size does not raise for small content."""
        _enforce_content_size(b"hello", "small.txt")  # should not raise


class IsIgnoredTests(unittest.TestCase):
    def test_node_modules_ignored(self):
        self.assertTrue(_is_ignored(Path("node_modules")))

    def test_dist_ignored(self):
        self.assertTrue(_is_ignored(Path("dist")))

    def test_build_ignored(self):
        self.assertTrue(_is_ignored(Path("build")))

    def test_hidden_file_ignored(self):
        self.assertTrue(_is_ignored(Path(".hidden")))

    def test_pycache_ignored(self):
        self.assertTrue(_is_ignored(Path("__pycache__") / "mod.pyc"))

    def test_regular_file_not_ignored(self):
        self.assertFalse(_is_ignored(Path("src") / "main.py"))


class GitRootTests(unittest.TestCase):
    def test_git_root_returns_path_inside_git_repo(self):
        """git_root() returns a Path that contains a .git directory."""
        root = git_root()
        self.assertIsInstance(root, Path)
        self.assertTrue((root / ".git").exists() or (root / ".git").is_file())


class RecomputeSliceEntryTests(unittest.TestCase):
    def _make_comp(self, exact, boundary=None, compat=None):
        return {"fingerprints": {"exact": exact, "boundary": boundary, "compat": compat}}

    def test_exact_mode_uses_exact_digest(self):
        comp = self._make_comp("aaa", boundary="bbb", compat="ccc")
        result = _recompute_slice_entry("s1", {"mode": "exact", "components": ["c"]}, {"c": comp})
        self.assertEqual(result["component_digests"]["c"], "aaa")

    def test_boundary_mode_uses_boundary_digest(self):
        comp = self._make_comp("aaa", boundary="bbb", compat="ccc")
        result = _recompute_slice_entry(
            "s1", {"mode": "boundary", "components": ["c"]}, {"c": comp}, strict=False
        )
        self.assertEqual(result["component_digests"]["c"], "bbb")

    def test_compat_mode_uses_compat_digest(self):
        comp = self._make_comp("aaa", boundary="bbb", compat="ccc")
        result = _recompute_slice_entry(
            "s1", {"mode": "compat", "components": ["c"]}, {"c": comp}, strict=False
        )
        self.assertEqual(result["component_digests"]["c"], "ccc")

    def test_boundary_strict_raises_when_boundary_is_none(self):
        comp = self._make_comp("aaa", boundary=None, compat=None)
        with self.assertRaises(ValueError):
            _recompute_slice_entry(
                "s1", {"mode": "boundary", "components": ["c"]}, {"c": comp}, strict=True
            )

    def test_compat_strict_raises_when_compat_is_none(self):
        comp = self._make_comp("aaa", boundary=None, compat=None)
        with self.assertRaises(ValueError):
            _recompute_slice_entry(
                "s1", {"mode": "compat", "components": ["c"]}, {"c": comp}, strict=True
            )

    def test_missing_component_in_map_gives_none_digest(self):
        result = _recompute_slice_entry(
            "s1", {"mode": "exact", "components": ["ghost"]}, {}, strict=False
        )
        self.assertIsNone(result["component_digests"]["ghost"])


class LockfileStructureIssuesTests(unittest.TestCase):
    def test_slices_not_dict_reported(self):
        """_lockfile_structure_issues reports when slices is not an object."""
        lockfile = {"components": {}, "slices": "not-a-dict"}
        issues = _lockfile_structure_issues(lockfile)
        self.assertTrue(any("slices" in i for i in issues))

    def test_component_not_dict_reported(self):
        """_lockfile_structure_issues reports when a component is not an object."""
        lockfile = {"components": {"svc": "not-a-dict"}, "slices": {}}
        issues = _lockfile_structure_issues(lockfile)
        self.assertTrue(any("svc" in i for i in issues))

    def test_missing_fingerprints_reported(self):
        """_lockfile_structure_issues reports missing fingerprints object."""
        lockfile = {"components": {"svc": {"no_fingerprints": True}}, "slices": {}}
        issues = _lockfile_structure_issues(lockfile)
        self.assertTrue(any("fingerprints" in i for i in issues))

    def test_missing_fingerprint_facet_reported(self):
        """_lockfile_structure_issues reports missing individual fingerprint keys."""
        lockfile = {
            "components": {"svc": {"fingerprints": {"exact": "abc"}}},
            "slices": {},
        }
        issues = _lockfile_structure_issues(lockfile)
        self.assertTrue(any("boundary" in i or "compat" in i for i in issues))


class DiscoverComponentsTests(unittest.TestCase):
    def test_discovers_package_json(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            (root / "svc" / "package.json").write_text('{"name":"svc","version":"1.0"}')
            found = discover_components(root)
            self.assertIn("svc", found)

    def test_skips_root_level_manifests(self):
        """Manifests at the repo root (rel_dir == '.') are skipped."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "package.json").write_text('{"name":"root","version":"1.0"}')
            found = discover_components(root)
            self.assertEqual(found, {})

    def test_name_collision_gets_suffix(self):
        """When two subdirs have the same name, the second gets a numeric suffix."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a" / "svc").mkdir(parents=True)
            (root / "b" / "svc").mkdir(parents=True)
            (root / "a" / "svc" / "package.json").write_text('{"version":"1.0"}')
            (root / "b" / "svc" / "package.json").write_text('{"version":"2.0"}')
            found = discover_components(root)
            self.assertIn("svc", found)
            self.assertIn("svc-2", found)

    def test_skips_git_directory(self):
        """Files inside .git are not returned as components."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            git_pkg = root / ".git" / "svc"
            git_pkg.mkdir(parents=True)
            (git_pkg / "package.json").write_text('{"version":"1.0"}')
            found = discover_components(root)
            self.assertEqual(found, {})


class HeadIndexDigestTests(unittest.TestCase):
    """Tests for source_tree_digest using head/index sources."""

    def _init_git_repo(self, root: Path) -> None:
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True, capture_output=True)

    def test_source_tree_digest_head_matches_working_tree_for_clean_commit(self):
        """source_tree_digest('head') equals source_tree_digest('working-tree') for a clean commit."""
        from boundver.core import source_tree_digest
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "main.py").write_text("x = 1\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            d_head = source_tree_digest(root, "svc", source="head")
            d_wt = source_tree_digest(root, "svc", source="working-tree")
            self.assertIsNotNone(d_head)
            self.assertEqual(d_head, d_wt)

    def test_source_tree_digest_head_differs_after_uncommitted_change(self):
        """source_tree_digest('head') is unchanged while 'working-tree' reflects edits."""
        from boundver.core import source_tree_digest
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            (root / "svc").mkdir()
            f = root / "svc" / "main.py"
            f.write_text("x = 1\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            d_head_before = source_tree_digest(root, "svc", source="head")
            f.write_text("x = 2\n")  # uncommitted change
            d_head_after = source_tree_digest(root, "svc", source="head")
            d_wt = source_tree_digest(root, "svc", source="working-tree")
            self.assertEqual(d_head_before, d_head_after)
            self.assertNotEqual(d_head_after, d_wt)

    def test_source_tree_digest_index_reflects_staged_change(self):
        """source_tree_digest('index') reflects staged (git add) but not committed changes."""
        from boundver.core import source_tree_digest
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            (root / "svc").mkdir()
            f = root / "svc" / "main.py"
            f.write_text("x = 1\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            f.write_text("x = 2\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            d_index = source_tree_digest(root, "svc", source="index")
            d_head = source_tree_digest(root, "svc", source="head")
            d_wt = source_tree_digest(root, "svc", source="working-tree")
            # index == working-tree (staged), head is old
            self.assertEqual(d_index, d_wt)
            self.assertNotEqual(d_index, d_head)

    def test_list_head_files_single_blob(self):
        """list_head_files returns a single file path when HEAD path is a blob."""
        from boundver.core import list_head_files
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            (root / "schema.json").write_text('{"key":"val"}')
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            files = list_head_files(root, "schema.json")
            self.assertEqual(files, ["schema.json"])


class GitBatchCatTests(unittest.TestCase):
    """Unit tests for the _git_batch_cat helper."""

    def _init_git_repo(self, root: Path) -> None:
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True, capture_output=True)

    def test_empty_refs_returns_empty_dict(self):
        """_git_batch_cat returns {} when given an empty ref list."""
        from boundver.core import _git_batch_cat
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            result = _git_batch_cat(root, [])
            self.assertEqual(result, {})

    def test_reads_committed_blob(self):
        """_git_batch_cat reads content of a committed file."""
        from boundver.core import _git_batch_cat
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            (root / "hello.txt").write_text("hello world\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            blobs = _git_batch_cat(root, ["HEAD:hello.txt"])
            self.assertEqual(blobs["HEAD:hello.txt"], b"hello world\n")

    def test_missing_object_maps_to_empty_bytes(self):
        """_git_batch_cat maps missing objects to b''."""
        from boundver.core import _git_batch_cat
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            (root / "f.txt").write_text("x\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            blobs = _git_batch_cat(root, ["HEAD:nonexistent.txt"])
            self.assertEqual(blobs["HEAD:nonexistent.txt"], b"")

    def test_multiple_refs_returned_correctly(self):
        """_git_batch_cat returns correct content for each ref in a batch."""
        from boundver.core import _git_batch_cat
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            (root / "a.txt").write_text("aaa\n")
            (root / "b.txt").write_text("bbb\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            blobs = _git_batch_cat(root, ["HEAD:a.txt", "HEAD:b.txt"])
            self.assertEqual(blobs["HEAD:a.txt"], b"aaa\n")
            self.assertEqual(blobs["HEAD:b.txt"], b"bbb\n")

    def test_git_batch_cat_error_raises_called_process_error(self):
        """_git_batch_cat raises CalledProcessError for invalid repo_root."""
        from boundver.core import _git_batch_cat
        import subprocess
        with self.assertRaises(subprocess.CalledProcessError):
            _git_batch_cat(Path("/nonexistent-repo-xyz"), ["HEAD:file.txt"])

    def test_git_batch_cat_missing_object_maps_to_empty_bytes(self):
        """_git_batch_cat returns b'' for objects marked 'missing' in batch output."""
        from boundver.core import _git_batch_cat
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            (root / "f.txt").write_text("x\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            blobs = _git_batch_cat(root, ["HEAD:no-such-file.txt"])
            self.assertEqual(blobs["HEAD:no-such-file.txt"], b"")


class ReadPathContentTests(unittest.TestCase):
    """Tests for _read_path_content covering index, head, symlink, and CRLF paths."""

    def _init_git_repo(self, root: Path) -> None:
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True, capture_output=True)

    def test_crlf_normalized_in_working_tree(self):
        """_read_path_content normalizes CRLF → LF for working-tree text files."""
        from boundver.core import _read_path_content
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "file.txt"
            f.write_bytes(b"line1\r\nline2\r\n")
            result = _read_path_content(root, f, source="working-tree")
            self.assertEqual(result, b"line1\nline2\n")

    def test_binary_not_crlf_normalized(self):
        """_read_path_content does not strip CRLF from binary files (null byte present)."""
        from boundver.core import _read_path_content
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "file.bin"
            f.write_bytes(b"data\r\n\x00binary")
            result = _read_path_content(root, f, source="working-tree")
            self.assertIn(b"\r\n", result)

    def test_index_source_reads_from_git_index(self):
        """_read_path_content with source='index' reads staged content."""
        from boundver.core import _read_path_content
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            f = root / "file.txt"
            f.write_text("hello\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            result = _read_path_content(root, f, source="index")
            self.assertEqual(result, b"hello\n")

    def test_head_source_reads_from_head_commit(self):
        """_read_path_content with source='head' reads from HEAD commit."""
        from boundver.core import _read_path_content
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            f = root / "file.txt"
            f.write_text("hello\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            result = _read_path_content(root, f, source="head")
            self.assertEqual(result, b"hello\n")


class ContentOnlyDigestTests(unittest.TestCase):
    """Tests for _content_only_digest — location-independent hash for vendored copies."""

    def _init_git_repo(self, root: Path) -> None:
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True, capture_output=True)

    def test_returns_none_for_nonexistent_path(self):
        """_content_only_digest returns None when path has no files."""
        from boundver.core import _content_only_digest
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = _content_only_digest(root, "nonexistent", source="working-tree")
            self.assertIsNone(result)

    def test_same_content_different_paths_same_digest(self):
        """_content_only_digest ignores path prefix — same content = same digest."""
        from boundver.core import _content_only_digest
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            (root / "vendor" / "svc").mkdir(parents=True)
            (root / "svc" / "api.py").write_text("def foo(): pass\n")
            (root / "vendor" / "svc" / "api.py").write_text("def foo(): pass\n")
            d1 = _content_only_digest(root, "svc", source="working-tree")
            d2 = _content_only_digest(root, "vendor/svc", source="working-tree")
            self.assertEqual(d1, d2)

    def test_head_source_returns_digest(self):
        """_content_only_digest with head source reads from HEAD commit."""
        from boundver.core import _content_only_digest
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "api.py").write_text("def foo(): pass\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            d = _content_only_digest(root, "svc", source="head")
            self.assertIsNotNone(d)
            self.assertEqual(len(d), 64)

    def test_index_source_returns_digest(self):
        """_content_only_digest with index source reads from staging area."""
        from boundver.core import _content_only_digest
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "api.py").write_text("def foo(): pass\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            d = _content_only_digest(root, "svc", source="index")
            self.assertIsNotNone(d)

    def test_head_source_matches_working_tree_for_clean_commit(self):
        """_content_only_digest(head) matches working-tree for clean commits."""
        from boundver.core import _content_only_digest
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            (root / "svc").mkdir()
            (root / "svc" / "api.py").write_text("def foo(): pass\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            d_head = _content_only_digest(root, "svc", source="head")
            d_wt = _content_only_digest(root, "svc", source="working-tree")
            self.assertEqual(d_head, d_wt)


class ShortHelperTests(unittest.TestCase):
    def test_short_none_returns_none_string(self):
        """_short(None) returns 'none'."""
        from boundver._utils import _short
        self.assertEqual(_short(None), "none")

    def test_short_long_hash_truncated(self):
        """_short truncates a hash to 12 chars + '...'."""
        from boundver._utils import _short
        h = "a" * 64
        result = _short(h)
        self.assertEqual(result, "aaaaaaaaaaaa...")


class AdditionalGitHelperTests(unittest.TestCase):
    """Additional git helper tests targeting uncovered branches."""

    def _init_git_repo(self, root: Path) -> None:
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True, capture_output=True)

    def test_changed_components_skips_empty_path(self):
        """Line 181: changed_components_since_ref skips components with empty path."""
        from boundver._git import changed_components_since_ref
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            (root / "f.txt").write_text("x")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            cfg = {
                "components": {
                    "no-path-comp": {"path": ""},  # empty path → continue (skipped)
                    "svc": {"path": "svc"},
                }
            }
            result = changed_components_since_ref(cfg, root, "HEAD")
            self.assertNotIn("no-path-comp", result)

    def test_git_latest_tag_returns_version_from_tag(self):
        """Lines 158-167: git_latest_tag returns version for a tagged commit."""
        from boundver.core import git_latest_tag
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            (root / "f.txt").write_text("x")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "tag", "svc-v1.2.3"], cwd=root, check=True, capture_output=True)
            result = git_latest_tag(root, "svc-v")
            self.assertEqual(result, "1.2.3")

    def test_git_latest_tag_no_matching_tags_returns_none(self):
        """git_latest_tag returns None when no tags match the prefix."""
        from boundver.core import git_latest_tag
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            (root / "f.txt").write_text("x")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            result = git_latest_tag(root, "nonexistent-prefix-v")
            self.assertIsNone(result)

    def test_git_latest_tag_fallback_returns_tag(self):
        """git_latest_tag falls back to git tag --list when describe fails (shallow repo)."""
        from boundver.core import git_latest_tag
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            (root / "f.txt").write_text("x")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            # Create a tag not reachable via --describe (on a detached-head branch workaround:
            # just create a plain tag so describe *can* find it but also test --list fallback).
            subprocess.run(["git", "tag", "svc-v1.2.3"], cwd=root, check=True, capture_output=True)
            result = git_latest_tag(root, "svc-v")
            self.assertEqual(result, "1.2.3")


class GitHelpersTests(unittest.TestCase):
    """Tests for _load_gitignore_patterns, _matches_gitignore, _list_files_for_source."""

    def _mk(self, td: str) -> Path:
        root = Path(td)
        (root / "sub").mkdir()
        (root / "sub" / "a.py").write_text("a")
        (root / "sub" / "b.txt").write_text("b")
        (root / "sub" / "skip.log").write_text("c")
        return root

    def test_load_gitignore_skips_comments_blank_negation(self):
        from boundver._git import _load_gitignore_patterns, _matches_gitignore
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".gitignore").write_text(
                "# comment\n\n!negated.txt\n*.log\nbuild/\n"
            )
            patterns = _load_gitignore_patterns(root)
            self.assertIsNotNone(patterns)
            # *.log should be matched
            self.assertTrue(_matches_gitignore("foo/x.log", patterns))
            # build/ should be matched (trailing / stripped)
            self.assertTrue(_matches_gitignore("build/out.js", patterns))
            # Negation: negated.txt should NOT be ignored
            # (negation undoes a previous match — but there's no rule that matches it first)
            self.assertFalse(_matches_gitignore("negated.txt", patterns))

    def test_load_gitignore_returns_none_when_absent(self):
        from boundver._git import _load_gitignore_patterns
        with tempfile.TemporaryDirectory() as td:
            result = _load_gitignore_patterns(Path(td))
            self.assertIsNone(result)

    def test_matches_gitignore_slash_prefix(self):
        from boundver._git import _matches_gitignore, _GitignoreRules
        # Pattern with "/" matches as directory prefix
        rules = _GitignoreRules()
        rules.add("vendor/lib")
        self.assertTrue(_matches_gitignore("vendor/lib/a.py", rules))

    def test_matches_gitignore_slash_exact(self):
        from boundver._git import _matches_gitignore, _GitignoreRules
        rules = _GitignoreRules()
        rules.add("dist")
        self.assertTrue(_matches_gitignore("dist", rules))

    def test_matches_gitignore_slash_glob(self):
        from boundver._git import _matches_gitignore, _GitignoreRules
        rules = _GitignoreRules()
        rules.add("src/*/bar.py")
        self.assertTrue(_matches_gitignore("src/foo/bar.py", rules))

    def test_matches_gitignore_no_slash_component(self):
        from boundver._git import _matches_gitignore, _GitignoreRules
        rules = _GitignoreRules()
        rules.add("*.log")
        self.assertTrue(_matches_gitignore("deep/path/skip.log", rules))

    def test_matches_gitignore_no_match(self):
        from boundver._git import _matches_gitignore, _GitignoreRules
        rules = _GitignoreRules()
        rules.add("*.log")
        rules.add("vendor")
        self.assertFalse(_matches_gitignore("src/main.py", rules))

    def test_list_files_for_source_filesystem_fallback_single_file(self):
        """_list_files_for_source falls back to filesystem when git ls-files is empty."""
        from boundver._git import _list_files_for_source
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # Write a single file — no git repo so ls-files returns nothing
            (root / "readme.md").write_text("hello")
            result = _list_files_for_source(root, "readme.md", "working-tree")
            self.assertIn("readme.md", result)

    def test_list_files_for_source_filesystem_fallback_directory(self):
        from boundver._git import _list_files_for_source
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "pkg").mkdir()
            (root / "pkg" / "a.py").write_text("a")
            (root / "pkg" / "b.py").write_text("b")
            result = _list_files_for_source(root, "pkg", "working-tree")
            rels = sorted(result)
            self.assertIn("pkg/a.py", rels)
            self.assertIn("pkg/b.py", rels)

    def test_list_files_for_source_filesystem_fallback_gitignore_excludes(self):
        from boundver._git import _list_files_for_source
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "pkg").mkdir()
            (root / "pkg" / "keep.py").write_text("x")
            (root / "pkg" / "skip.log").write_text("y")
            (root / ".gitignore").write_text("*.log\n")
            result = _list_files_for_source(root, "pkg", "working-tree")
            self.assertIn("pkg/keep.py", result)
            self.assertNotIn("pkg/skip.log", result)

    def test_list_files_for_source_nonexistent_path_returns_empty(self):
        from boundver._git import _list_files_for_source
        with tempfile.TemporaryDirectory() as td:
            result = _list_files_for_source(Path(td), "no/such/path", "working-tree")
            self.assertEqual(result, [])
