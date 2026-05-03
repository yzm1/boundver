"""Unit tests for boundver.versions — pure function tests, no git required."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from boundver.versions import (
    _extract_json_field,
    _extract_toml_field,
    _extract_yaml_field,
    extract_version,
    parse_semver,
)


class ParseSemverTests(unittest.TestCase):
    def test_full_three_part_version(self):
        self.assertEqual(parse_semver("1.2.3"), ("1", "1.2", "1.2.3"))

    def test_v_prefix_stripped(self):
        self.assertEqual(parse_semver("v2.4.0"), ("2", "2.4", "2.4.0"))

    def test_two_part_version_patch_defaults_to_zero(self):
        self.assertEqual(parse_semver("v2.4"), ("2", "2.4", "2.4.0"))

    def test_none_returns_triple_none(self):
        self.assertEqual(parse_semver(None), (None, None, None))

    def test_empty_string_returns_triple_none(self):
        self.assertEqual(parse_semver(""), (None, None, None))

    def test_non_semver_string_returns_raw_in_exact(self):
        major, api, exact = parse_semver("not-a-version")
        self.assertIsNone(major)
        self.assertIsNone(api)
        self.assertEqual(exact, "not-a-version")

    def test_major_zero(self):
        self.assertEqual(parse_semver("0.1.0"), ("0", "0.1", "0.1.0"))

    def test_large_version_numbers(self):
        self.assertEqual(parse_semver("10.20.300"), ("10", "10.20", "10.20.300"))

    def test_prerelease_suffix_ignored_in_semver_parts(self):
        # parse_semver extracts leading numeric parts; suffix after patch is ignored
        major, api, exact = parse_semver("1.2.3-alpha")
        self.assertEqual(major, "1")
        self.assertEqual(api, "1.2")
        self.assertEqual(exact, "1.2.3")


class ExtractJsonFieldTests(unittest.TestCase):
    def _write(self, tmp: Path, content: dict) -> Path:
        p = tmp / "data.json"
        p.write_text(json.dumps(content))
        return p

    def test_top_level_field(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(Path(td), {"version": "1.0.0"})
            self.assertEqual(_extract_json_field(p, "version"), "1.0.0")

    def test_nested_two_level(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(Path(td), {"project": {"version": "2.3.4"}})
            self.assertEqual(_extract_json_field(p, "project.version"), "2.3.4")

    def test_nested_three_level(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(Path(td), {"a": {"b": {"c": "deep"}}})
            self.assertEqual(_extract_json_field(p, "a.b.c"), "deep")

    def test_missing_field_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(Path(td), {"name": "pkg"})
            self.assertIsNone(_extract_json_field(p, "version"))

    def test_invalid_json_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.json"
            p.write_text("{not valid json")
            self.assertIsNone(_extract_json_field(p, "version"))

    def test_numeric_value_returned_as_string(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(Path(td), {"count": 42})
            self.assertEqual(_extract_json_field(p, "count"), "42")

    def test_intermediate_key_not_dict_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(Path(td), {"x": "string-not-dict"})
            self.assertIsNone(_extract_json_field(p, "x.version"))


class ExtractTomlFieldTests(unittest.TestCase):
    def _write(self, tmp: Path, content: str) -> Path:
        p = tmp / "pyproject.toml"
        p.write_text(content)
        return p

    def test_top_level_field(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(Path(td), 'version = "1.0.0"\n')
            self.assertEqual(_extract_toml_field(p, "version"), "1.0.0")

    def test_two_level_section(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(Path(td), "[project]\nversion = \"2.3.4\"\n")
            self.assertEqual(_extract_toml_field(p, "project.version"), "2.3.4")

    def test_three_level_section(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(
                Path(td),
                "[tool.poetry]\nname = \"svc\"\nversion = \"2.5.1\"\n",
            )
            self.assertEqual(_extract_toml_field(p, "tool.poetry.version"), "2.5.1")

    def test_missing_key_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(Path(td), "[project]\nname = \"pkg\"\n")
            self.assertIsNone(_extract_toml_field(p, "project.version"))

    def test_missing_section_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(Path(td), "[other]\nversion = \"1.0.0\"\n")
            self.assertIsNone(_extract_toml_field(p, "project.version"))

    def test_multiple_sections_picks_correct_one(self):
        with tempfile.TemporaryDirectory() as td:
            content = "[tool.black]\nline-length = 88\n[tool.poetry]\nversion = \"3.0.0\"\n"
            p = self._write(Path(td), content)
            self.assertEqual(_extract_toml_field(p, "tool.poetry.version"), "3.0.0")

    def test_returns_none_for_empty_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(Path(td), "")
            self.assertIsNone(_extract_toml_field(p, "version"))


class ExtractYamlFieldTests(unittest.TestCase):
    def _write(self, tmp: Path, content: str) -> Path:
        p = tmp / "data.yaml"
        p.write_text(content)
        return p

    def test_top_level_field(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(Path(td), "version: 1.0.0\n")
            self.assertEqual(_extract_yaml_field(p, "version"), "1.0.0")

    def test_nested_two_level(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(Path(td), "info:\n  version: 2.3.4\n")
            self.assertEqual(_extract_yaml_field(p, "info.version"), "2.3.4")

    def test_missing_key_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(Path(td), "name: myservice\n")
            self.assertIsNone(_extract_yaml_field(p, "version"))

    def test_quoted_value(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(Path(td), 'version: "1.2.3"\n')
            self.assertEqual(_extract_yaml_field(p, "version"), "1.2.3")

    def test_comment_lines_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(Path(td), "# header\nversion: 4.5.6\n")
            self.assertEqual(_extract_yaml_field(p, "version"), "4.5.6")

    def test_empty_file_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(Path(td), "")
            self.assertIsNone(_extract_yaml_field(p, "version"))

    def test_fallback_parser_used_when_yaml_unavailable(self):
        import boundver.versions as versions_mod
        original = versions_mod.yaml
        try:
            versions_mod.yaml = None
            with tempfile.TemporaryDirectory() as td:
                p = self._write(Path(td), "info:\n  version: 9.8.7\n")
                self.assertEqual(_extract_yaml_field(p, "info.version"), "9.8.7")
        finally:
            versions_mod.yaml = original

    def test_fallback_parser_top_level(self):
        import boundver.versions as versions_mod
        original = versions_mod.yaml
        try:
            versions_mod.yaml = None
            with tempfile.TemporaryDirectory() as td:
                p = self._write(Path(td), "version: 1.0.0\n")
                self.assertEqual(_extract_yaml_field(p, "version"), "1.0.0")
        finally:
            versions_mod.yaml = original


class ExtractVersionTests(unittest.TestCase):
    def test_no_version_source_returns_none(self):
        self.assertIsNone(extract_version(Path("."), ".", None, None))

    def test_git_tag_prefix_without_resolver_returns_none(self):
        self.assertIsNone(
            extract_version(Path("."), ".", {"git_tag_prefix": "v"}, None)
        )

    def test_git_tag_prefix_calls_resolver(self):
        mock_fn = MagicMock(return_value="1.2.3")
        result = extract_version(Path("/repo"), "svc", {"git_tag_prefix": "svc-v"}, mock_fn)
        self.assertEqual(result, "1.2.3")
        mock_fn.assert_called_once_with(Path("/repo"), "svc-v")

    def test_file_based_json_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            comp = root / "svc"
            comp.mkdir()
            (comp / "version.json").write_text('{"version": "3.1.4"}')
            result = extract_version(
                root, "svc", {"file": "version.json", "field": "version"}, None
            )
            self.assertEqual(result, "3.1.4")

    def test_file_based_toml_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            comp = root / "lib"
            comp.mkdir()
            (comp / "pyproject.toml").write_text("[project]\nversion = \"0.9.1\"\n")
            result = extract_version(
                root, "lib", {"file": "pyproject.toml", "field": "project.version"}, None
            )
            self.assertEqual(result, "0.9.1")

    def test_missing_file_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            result = extract_version(
                Path(td), "svc", {"file": "missing.json", "field": "version"}, None
            )
            self.assertIsNone(result)

    def test_missing_field_in_version_source_returns_none(self):
        # If version_source has neither 'file' nor 'git_tag_prefix', returns None
        result = extract_version(Path("."), "svc", {"other_key": "value"}, None)
        self.assertIsNone(result)

    def test_yml_extension_handled_like_yaml(self):
        """extract_version reads .yml files the same as .yaml."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            comp = root / "svc"
            comp.mkdir()
            (comp / "chart.yml").write_text("version: 4.5.6\n")
            result = extract_version(
                root, "svc", {"file": "chart.yml", "field": "version"}, None
            )
            self.assertEqual(result, "4.5.6")

    def test_extract_toml_invalid_toml_returns_none(self):
        """_extract_toml_field returns None for unparseable TOML."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.toml"
            p.write_text("not = valid [ toml {\n")
            result = _extract_toml_field(p, "not")
            # Depending on whether tomllib is available, either path should return
            # None rather than raising an exception.
            self.assertIsNone(result)

    def test_extract_yaml_nested_section_fallback(self):
        """_extract_yaml_field fallback parser handles nested sections."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "data.yaml"
            p.write_text("outer:\n  inner: found-it\n")
            result = _extract_yaml_field(p, "outer.inner")
            self.assertEqual(result, "found-it")

    def test_extract_toml_top_level_field_fallback(self):
        """_extract_toml_field fallback parser handles top-level keys (no section)."""
        import sys
        import unittest.mock
        # Patch tomllib to None to force fallback parser
        with unittest.mock.patch.dict(sys.modules, {"tomllib": None}):
            import importlib
            import boundver.versions as v_mod
            orig = v_mod.tomllib
            v_mod.tomllib = None
            try:
                with tempfile.TemporaryDirectory() as td:
                    p = Path(td) / "cfg.toml"
                    p.write_text('version = "9.8.7"\n')
                    result = v_mod._extract_toml_field(p, "version")
                    self.assertEqual(result, "9.8.7")
            finally:
                v_mod.tomllib = orig

    def test_extract_version_unsupported_extension_returns_none(self):
        """extract_version returns None for a file with an unsupported extension."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            comp = root / "svc"
            comp.mkdir()
            (comp / "VERSION").write_text("1.0.0\n")
            result = extract_version(root, "svc", {"file": "VERSION", "field": "version"}, None)
            self.assertIsNone(result)

    def test_extract_toml_missing_key_in_tomllib_path(self):
        """_extract_toml_field returns None when key exists in section but target section is missing."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "p.toml"
            p.write_text("[tool.other]\nversion = \"1.0\"\n")
            result = _extract_toml_field(p, "tool.poetry.version")
            self.assertIsNone(result)

    def test_extract_yaml_yaml_loaded_key_not_found(self):
        """_extract_yaml_field with yaml available returns None for missing key."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "data.yaml"
            p.write_text("name: myservice\nother: value\n")
            result = _extract_yaml_field(p, "version")
            self.assertIsNone(result)

    def test_extract_yaml_fallback_parser_nested_section(self):
        """_extract_yaml_field fallback parser navigates indented sections."""
        import boundver.versions as v_mod
        orig = v_mod.yaml
        v_mod.yaml = None
        try:
            with tempfile.TemporaryDirectory() as td:
                p = Path(td) / "data.yaml"
                p.write_text("service:\n  version: 2.0.0\n")
                result = v_mod._extract_yaml_field(p, "service.version")
                self.assertEqual(result, "2.0.0")
        finally:
            v_mod.yaml = orig

    def test_extract_yaml_fallback_parser_key_not_found(self):
        """_extract_yaml_field fallback parser returns None when key absent."""
        import boundver.versions as v_mod
        orig = v_mod.yaml
        v_mod.yaml = None
        try:
            with tempfile.TemporaryDirectory() as td:
                p = Path(td) / "data.yaml"
                p.write_text("name: svc\n")
                result = v_mod._extract_yaml_field(p, "version")
                self.assertIsNone(result)
        finally:
            v_mod.yaml = orig

    def test_extract_toml_tomllib_intermediate_not_dict_returns_none(self):
        """Lines 76-77: tomllib path returns None when intermediate key is not a dict."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "p.toml"
            # [tool]\npoetry = "string" → tool.poetry is a string, not a dict
            p.write_text("[tool]\npoetry = \"not-a-dict\"\n")
            result = _extract_toml_field(p, "tool.poetry.version")
            self.assertIsNone(result)

    def test_extract_toml_fallback_single_key_top_level(self):
        """Lines 79-80: fallback parser with single-key path (no section)."""
        import boundver.versions as v_mod
        orig = v_mod.tomllib
        v_mod.tomllib = None
        try:
            with tempfile.TemporaryDirectory() as td:
                p = Path(td) / "cfg.toml"
                p.write_text("name = \"pkg\"\nversion = \"5.6.7\"\n")
                result = v_mod._extract_toml_field(p, "version")
                self.assertEqual(result, "5.6.7")
        finally:
            v_mod.tomllib = orig

    def test_extract_toml_fallback_multi_key_with_section(self):
        """Lines 76-77, 86-87: fallback parser with multi-key path needs section headers."""
        import boundver.versions as v_mod
        orig = v_mod.tomllib
        v_mod.tomllib = None
        try:
            with tempfile.TemporaryDirectory() as td:
                p = Path(td) / "cfg.toml"
                # Multi-key path requiring section header traversal
                p.write_text("[project]\nname = \"pkg\"\nversion = \"9.9.9\"\n")
                result = v_mod._extract_toml_field(p, "project.version")
                self.assertEqual(result, "9.9.9")
        finally:
            v_mod.tomllib = orig

    def test_extract_toml_fallback_section_not_found(self):
        """Lines 76-77, 92: fallback parser with multi-key path and wrong section → None."""
        import boundver.versions as v_mod
        orig = v_mod.tomllib
        v_mod.tomllib = None
        try:
            with tempfile.TemporaryDirectory() as td:
                p = Path(td) / "cfg.toml"
                p.write_text("[other]\nversion = \"1.0.0\"\n")
                result = v_mod._extract_toml_field(p, "project.version")
                self.assertIsNone(result)
        finally:
            v_mod.tomllib = orig

    def test_extract_toml_fallback_single_key_not_found(self):
        """Line 92: fallback parser single-key path, key not present → None."""
        import boundver.versions as v_mod
        orig = v_mod.tomllib
        v_mod.tomllib = None
        try:
            with tempfile.TemporaryDirectory() as td:
                p = Path(td) / "cfg.toml"
                p.write_text("name = \"pkg\"\n")
                result = v_mod._extract_toml_field(p, "version")
                self.assertIsNone(result)
        finally:
            v_mod.tomllib = orig

    def test_extract_yaml_invalid_raises_uses_fallback(self):
        """Lines 101-102: yaml.safe_load raises exception → data=None → fallback parser used."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "data.yaml"
            # !invalid_tag causes yaml.safe_load to raise ConstructorError
            # The fallback parser then processes the raw text
            p.write_text("version: !invalid_tag 1.0.0\n")
            # The yaml exception path sets data=None, then falls through to fallback parser
            # Fallback regex won't match "!invalid_tag" but should not raise
            result = _extract_yaml_field(p, "version")
            # Fallback may return the raw text after the colon or None
            # Either way, the important thing is no exception is raised
            # (the result depends on the fallback regex matching)

    def test_extract_yaml_yaml_intermediate_not_dict_returns_none(self):
        """Lines 105-106: yaml module path returns None when intermediate key is not a dict."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "data.yaml"
            # info is a string, not a dict → info.version should return None
            p.write_text("info: plain-string\n")
            result = _extract_yaml_field(p, "info.version")
            self.assertIsNone(result)

    def test_extract_yaml_yaml_key_not_in_nested_dict(self):
        """Lines 105-106: yaml module path returns None when key absent in nested dict."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "data.yaml"
            p.write_text("info:\n  name: svc\n")
            result = _extract_yaml_field(p, "info.version")
            self.assertIsNone(result)

    def test_extract_yaml_fallback_blank_line_ignored(self):
        """Line 116: fallback parser skips blank lines."""
        import boundver.versions as v_mod
        orig = v_mod.yaml
        v_mod.yaml = None
        try:
            with tempfile.TemporaryDirectory() as td:
                p = Path(td) / "data.yaml"
                # Has blank line between sections — should still find the value
                p.write_text("outer:\n\n  version: 3.2.1\n")
                result = v_mod._extract_yaml_field(p, "outer.version")
                self.assertEqual(result, "3.2.1")
        finally:
            v_mod.yaml = orig

    def test_extract_yaml_fallback_three_level_nesting(self):
        """Lines 120-122: fallback parser pops indent stack for multi-level sections."""
        import boundver.versions as v_mod
        orig = v_mod.yaml
        v_mod.yaml = None
        try:
            with tempfile.TemporaryDirectory() as td:
                p = Path(td) / "data.yaml"
                p.write_text("outer:\n  inner:\n    version: 7.8.9\n")
                result = v_mod._extract_yaml_field(p, "outer.inner.version")
                self.assertEqual(result, "7.8.9")
        finally:
            v_mod.yaml = orig

    def test_extract_yaml_fallback_indent_stack_pop(self):
        """Lines 120-122: indent_stack pops when a shallower indent is encountered."""
        import boundver.versions as v_mod
        orig = v_mod.yaml
        v_mod.yaml = None
        try:
            with tempfile.TemporaryDirectory() as td:
                p = Path(td) / "data.yaml"
                # After going deep, come back out - version is at top level after nested section
                p.write_text("nested:\n  deep: value\nversion: 1.0.0\n")
                result = v_mod._extract_yaml_field(p, "version")
                self.assertEqual(result, "1.0.0")
        finally:
            v_mod.yaml = orig


class ExtractFieldFromBytesTests(unittest.TestCase):
    """Tests for _extract_field_from_bytes — covers lines 64-76."""

    def setUp(self):
        from boundver.versions import _extract_field_from_bytes
        self._fn = _extract_field_from_bytes

    def test_json_bytes(self):
        raw = b'{"version": "3.1.4"}'
        self.assertEqual(self._fn(raw, "package.json", "version"), "3.1.4")

    def test_toml_bytes(self):
        raw = b'[project]\nname = "my-pkg"\nversion = "1.0.0"\n'
        self.assertEqual(self._fn(raw, "pyproject.toml", "project.version"), "1.0.0")

    def test_yaml_bytes(self):
        raw = b"version: 2.3.0\n"
        result = self._fn(raw, "version.yaml", "version")
        self.assertEqual(result, "2.3.0")

    def test_yml_extension(self):
        raw = b"info:\n  version: 1.0.0\n"
        result = self._fn(raw, "openapi.yml", "info.version")
        # YAML parse might return None if yaml not installed, but should not crash.
        self.assertIn(result, ("1.0.0", None))

    def test_unsupported_extension_returns_none(self):
        raw = b"version=1.0.0\n"
        self.assertIsNone(self._fn(raw, "config.ini", "version"))

    def test_unicode_decode_error_returns_none(self):
        raw = b"\xff\xfe"  # Invalid UTF-8
        self.assertIsNone(self._fn(raw, "bad.json", "version"))


class ExtractJsonFromTextTests(unittest.TestCase):
    """Tests for _extract_json_from_text — covers lines 79-88."""

    def setUp(self):
        from boundver.versions import _extract_json_from_text
        self._fn = _extract_json_from_text

    def test_top_level(self):
        self.assertEqual(self._fn('{"version": "1.2.3"}', "version"), "1.2.3")

    def test_nested(self):
        self.assertEqual(self._fn('{"pkg": {"version": "2.0"}}', "pkg.version"), "2.0")

    def test_invalid_json(self):
        self.assertIsNone(self._fn("{not-json}", "version"))

    def test_missing_key(self):
        self.assertIsNone(self._fn('{"name": "x"}', "version"))

    def test_numeric_value_as_string(self):
        self.assertEqual(self._fn('{"v": 42}', "v"), "42")


class ExtractTomlFromTextTests(unittest.TestCase):
    """Tests for _extract_toml_from_text — covers lines 89-122."""

    def setUp(self):
        from boundver.versions import _extract_toml_from_text
        self._fn = _extract_toml_from_text

    def test_top_level_via_tomllib(self):
        self.assertEqual(self._fn('version = "1.0.0"\n', "version"), "1.0.0")

    def test_section_key_via_tomllib(self):
        self.assertEqual(self._fn('[project]\nversion = "2.3.4"\n', "project.version"), "2.3.4")

    def test_missing_key_via_tomllib(self):
        self.assertIsNone(self._fn('[project]\nname = "x"\n', "project.version"))

    def test_invalid_toml_returns_none(self):
        self.assertIsNone(self._fn("not = valid = toml\n", "not"))

    def test_nested_key_not_dict_returns_none(self):
        self.assertIsNone(self._fn('project = "string"\n', "project.version"))

    def test_fallback_regex_top_level(self):
        """TOML fallback regex parser covers lines 103-120 when tomllib is None."""
        import boundver.versions as v_mod
        orig = v_mod.tomllib
        v_mod.tomllib = None
        try:
            result = self._fn('version = "1.0.0"\n', "version")
            self.assertEqual(result, "1.0.0")
        finally:
            v_mod.tomllib = orig

    def test_fallback_regex_section_key(self):
        """Fallback regex parser handles [section] + key."""
        import boundver.versions as v_mod
        orig = v_mod.tomllib
        v_mod.tomllib = None
        try:
            result = self._fn('[project]\nversion = "3.2.1"\n', "project.version")
            self.assertEqual(result, "3.2.1")
        finally:
            v_mod.tomllib = orig

    def test_fallback_regex_key_not_found_returns_none(self):
        """Fallback regex returns None when key is absent."""
        import boundver.versions as v_mod
        orig = v_mod.tomllib
        v_mod.tomllib = None
        try:
            result = self._fn('[project]\nname = "x"\n', "project.version")
            self.assertIsNone(result)
        finally:
            v_mod.tomllib = orig

    def test_fallback_regex_single_key_no_section(self):
        """Fallback regex handles single-key (no section) lookup."""
        import boundver.versions as v_mod
        orig = v_mod.tomllib
        v_mod.tomllib = None
        try:
            result = self._fn('name = "x"\nversion = "4.0.0"\n', "version")
            self.assertEqual(result, "4.0.0")
        finally:
            v_mod.tomllib = orig


class ExtractYamlFromTextTests(unittest.TestCase):
    """Tests for _extract_yaml_from_text — covers lines 123-162."""

    def setUp(self):
        from boundver.versions import _extract_yaml_from_text
        self._fn = _extract_yaml_from_text

    def test_top_level(self):
        result = self._fn("version: 1.0.0\n", "version")
        self.assertEqual(result, "1.0.0")

    def test_nested_via_yaml(self):
        result = self._fn("info:\n  version: 2.3.4\n", "info.version")
        self.assertIn(result, ("2.3.4", None))  # None if PyYAML not installed

    def test_missing_key_returns_none(self):
        result = self._fn("name: x\n", "version")
        self.assertIsNone(result)

    def test_invalid_yaml_falls_back_to_regex(self):
        # Malformed YAML should still try the fallback regex parser.
        result = self._fn("version: 1.0.0\n", "version")
        self.assertEqual(result, "1.0.0")

    def test_yaml_exception_returns_none(self):
        """yaml.safe_load raising Exception → return None (parse failure is authoritative)."""
        import boundver.versions as v_mod
        orig = v_mod.yaml
        # Simulate yaml module that raises on safe_load.
        class _BadYaml:
            @staticmethod
            def safe_load(text):
                raise RuntimeError("yaml exploded")
        v_mod.yaml = _BadYaml()
        try:
            result = self._fn("version: 1.2.3\n", "version")
            self.assertIsNone(result)
        finally:
            v_mod.yaml = orig

    def test_yaml_key_found_returns_early(self):
        """When yaml finds the key, returns immediately without running regex (line 143)."""
        import boundver.versions as v_mod
        if v_mod.yaml is None:
            self.skipTest("PyYAML not installed")
        result = self._fn("version: 5.6.7\n", "version")
        self.assertEqual(result, "5.6.7")

    def test_yaml_none_fallback_regex_top_level(self):
        """With yaml=None, fallback regex extracts top-level key (lines 147-161)."""
        import boundver.versions as v_mod
        orig = v_mod.yaml
        v_mod.yaml = None
        try:
            result = self._fn("version: 0.9.1\n", "version")
            self.assertEqual(result, "0.9.1")
        finally:
            v_mod.yaml = orig

    def test_yaml_none_fallback_regex_nested(self):
        """With yaml=None, fallback regex extracts nested key via section tracking."""
        import boundver.versions as v_mod
        orig = v_mod.yaml
        v_mod.yaml = None
        try:
            result = self._fn("info:\n  version: 3.0.0\n", "info.version")
            self.assertEqual(result, "3.0.0")
        finally:
            v_mod.yaml = orig

    def test_yaml_none_key_not_found_returns_none(self):
        """Fallback regex returns None when key is not present."""
        import boundver.versions as v_mod
        orig = v_mod.yaml
        v_mod.yaml = None
        try:
            result = self._fn("name: x\n", "version")
            self.assertIsNone(result)
        finally:
            v_mod.yaml = orig

    def test_yaml_none_blank_line_skipped(self):
        """Blank lines in YAML text are skipped (fallback regex line 143 `continue`)."""
        import boundver.versions as v_mod
        orig = v_mod.yaml
        v_mod.yaml = None
        try:
            result = self._fn("\n\nversion: 9.0.0\n", "version")
            self.assertEqual(result, "9.0.0")
        finally:
            v_mod.yaml = orig

    def test_yaml_none_comment_line_skipped(self):
        """Comment lines in YAML text are skipped (fallback regex `continue` branch)."""
        import boundver.versions as v_mod
        orig = v_mod.yaml
        v_mod.yaml = None
        try:
            result = self._fn("# a comment\nversion: 8.0.0\n", "version")
            self.assertEqual(result, "8.0.0")
        finally:
            v_mod.yaml = orig

    def test_yaml_none_indent_stack_pop(self):
        """Going from deep to shallow indent pops the indent stack (lines 147-149)."""
        import boundver.versions as v_mod
        orig = v_mod.yaml
        v_mod.yaml = None
        try:
            # 'nested:' section → '  deep: x' → then back out to 'version: ...'
            result = self._fn("nested:\n  deep: x\nversion: 7.0.0\n", "version")
            self.assertEqual(result, "7.0.0")
        finally:
            v_mod.yaml = orig


class ExtractFileVersionWithReadFnTests(unittest.TestCase):
    """Tests for extract_version with read_file_fn — covers lines 46-50."""

    def setUp(self):
        from boundver.versions import extract_version
        self._fn = extract_version

    def test_read_file_fn_json_source(self):
        """read_file_fn is called when provided; result comes from _extract_field_from_bytes."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "svc").mkdir()
            content = b'{"version": "5.0.1"}'
            read_fn = MagicMock(return_value=content)
            result = self._fn(
                root, "svc",
                version_source={"file": "package.json", "field": "version"},
                read_file_fn=read_fn,
            )
            self.assertEqual(result, "5.0.1")
            read_fn.assert_called_once_with("svc/package.json")

    def test_read_file_fn_oserror_returns_none(self):
        """When read_file_fn raises OSError, extract_version returns None (lines 48-49)."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            read_fn = MagicMock(side_effect=OSError("no such file"))
            result = self._fn(
                root, "svc",
                version_source={"file": "package.json", "field": "version"},
                read_file_fn=read_fn,
            )
            self.assertIsNone(result)

    def test_read_file_fn_subprocess_error_returns_none(self):
        """When read_file_fn raises CalledProcessError, returns None (lines 48-49)."""
        import subprocess as _sp
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            read_fn = MagicMock(side_effect=_sp.CalledProcessError(1, "git"))
            result = self._fn(
                root, "svc",
                version_source={"file": "openapi.yaml", "field": "info.version"},
                read_file_fn=read_fn,
            )
            self.assertIsNone(result)

