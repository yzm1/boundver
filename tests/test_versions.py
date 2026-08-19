"""Unit tests for boundver.versions — pure function tests, no git required."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from boundver.versions import (
    _extract_json_field,
    _extract_toml_field,
    _extract_yaml_field,
    MAX_TOML_INTEGER_DIGITS,
    extract_version,
    parse_semver,
)
from boundver._utils import GuardrailError


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

    def test_prerelease_suffix_ignored_in_derived_semver_parts(self):
        major, api, exact = parse_semver("1.2.3-alpha")
        self.assertEqual(major, "1")
        self.assertEqual(api, "1.2")
        self.assertEqual(exact, "1.2.3")

    def test_trailing_junk_is_not_accepted_as_semver(self):
        major, api, exact = parse_semver("1.2.3-not valid")
        self.assertIsNone(major)
        self.assertIsNone(api)
        self.assertEqual(exact, "1.2.3-not valid")

    def test_build_metadata_is_valid_semver(self):
        self.assertEqual(
            parse_semver("1.2.3-alpha.1+build.9"),
            ("1", "1.2", "1.2.3"),
        )

    def test_unicode_digits_are_not_semver_numeric_identifiers(self):
        version = "1.2.\u0663"
        self.assertEqual(parse_semver(version), (None, None, version))

    def test_numeric_prerelease_identifiers_cannot_have_leading_zeroes(self):
        for version in ("1.2.3-01", "1.2.3-alpha.01"):
            with self.subTest(version=version):
                self.assertEqual(parse_semver(version), (None, None, version))
        self.assertEqual(parse_semver("1.2.3-0"), ("1", "1.2", "1.2.3"))
        self.assertEqual(
            parse_semver("1.2.3-01alpha"),
            ("1", "1.2", "1.2.3"),
        )

    def test_long_invalid_prerelease_is_rejected_without_regex_backtracking(self):
        version = "1.2.3-" + "a" * 100_000 + "!"
        self.assertEqual(parse_semver(version), (None, None, version))


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

    def test_bounded_reader_failure_is_a_controlled_missing_version(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(Path(td), {"version": "1.0.0"})
            with patch(
                "boundver.versions._read_bounded_path_bytes",
                side_effect=GuardrailError("file grew past the limit"),
            ):
                self.assertIsNone(_extract_json_field(p, "version"))


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

    def test_oversized_implicit_and_explicit_integers_return_none(self):
        for tagged_value in ("9" * 4301, "!!int " + "9" * 4301):
            with self.subTest(explicit=tagged_value.startswith("!!int")):
                with tempfile.TemporaryDirectory() as td:
                    path = self._write(
                        Path(td), f"version: {tagged_value}\n"
                    )
                    self.assertIsNone(_extract_yaml_field(path, "version"))

    def test_nested_yaml_fails_closed_when_parser_unavailable(self):
        import boundver.versions as versions_mod
        original = versions_mod.yaml
        try:
            versions_mod.yaml = None
            with tempfile.TemporaryDirectory() as td:
                p = self._write(Path(td), "info:\n  version: 9.8.7\n")
                self.assertIsNone(_extract_yaml_field(p, "info.version"))
        finally:
            versions_mod.yaml = original

    def test_top_level_yaml_fails_closed_when_parser_unavailable(self):
        import boundver.versions as versions_mod
        original = versions_mod.yaml
        try:
            versions_mod.yaml = None
            with tempfile.TemporaryDirectory() as td:
                p = self._write(Path(td), "version: 1.0.0\n")
                self.assertIsNone(_extract_yaml_field(p, "version"))
        finally:
            versions_mod.yaml = original


class ExtractVersionTests(unittest.TestCase):
    def test_no_version_source_returns_none(self):
        self.assertIsNone(extract_version(Path("."), ".", None, None))

    def test_malformed_version_source_and_component_path_fail_closed(self):
        resolver = MagicMock(return_value="1.2.3")
        for malformed in ([], "version.json", 1, True):
            with self.subTest(version_source=malformed):
                self.assertIsNone(
                    extract_version(Path("."), "svc", malformed, resolver)
                )
        for malformed_prefix in ("", 1, [], True):
            with self.subTest(git_tag_prefix=malformed_prefix):
                self.assertIsNone(
                    extract_version(
                        Path("."),
                        "svc",
                        {"git_tag_prefix": malformed_prefix},
                        resolver,
                    )
                )
        self.assertIsNone(
            extract_version(
                Path("."),
                1,
                {"file": "version.json", "field": "version"},
                resolver,
            )
        )
        resolver.assert_not_called()

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
            self.assertIsNone(result)

    def test_extract_yaml_nested_section(self):
        """The authoritative YAML parser handles nested sections."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "data.yaml"
            p.write_text("outer:\n  inner: found-it\n")
            result = _extract_yaml_field(p, "outer.inner")
            self.assertEqual(result, "found-it")

    def test_extract_toml_without_parser_fails_closed(self):
        """A missing TOML parser never triggers best-effort extraction."""
        import sys
        import unittest.mock
        # Simulate a broken installation without the required TOML parser.
        with unittest.mock.patch.dict(sys.modules, {"tomllib": None}):
            import boundver.versions as v_mod
            orig = v_mod.tomllib
            v_mod.tomllib = None
            try:
                with tempfile.TemporaryDirectory() as td:
                    p = Path(td) / "cfg.toml"
                    p.write_text('version = "9.8.7"\n')
                    result = v_mod._extract_toml_field(p, "version")
                    self.assertIsNone(result)
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

    def test_extract_nested_yaml_without_parser_fails_closed(self):
        """A missing YAML parser never triggers best-effort extraction."""
        import boundver.versions as v_mod
        orig = v_mod.yaml
        v_mod.yaml = None
        try:
            with tempfile.TemporaryDirectory() as td:
                p = Path(td) / "data.yaml"
                p.write_text("service:\n  version: 2.0.0\n")
                result = v_mod._extract_yaml_field(p, "service.version")
                self.assertIsNone(result)
        finally:
            v_mod.yaml = orig

    def test_extract_toml_tomllib_intermediate_not_dict_returns_none(self):
        """TOML traversal returns None when an intermediate key is not a mapping."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "p.toml"
            # [tool]\npoetry = "string" → tool.poetry is a string, not a dict
            p.write_text("[tool]\npoetry = \"not-a-dict\"\n")
            result = _extract_toml_field(p, "tool.poetry.version")
            self.assertIsNone(result)

    def test_extract_yaml_invalid_document_returns_none(self):
        """An invalid tagged YAML value is rejected authoritatively."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "data.yaml"
            p.write_text("version: !invalid_tag 1.0.0\n")
            self.assertIsNone(_extract_yaml_field(p, "version"))

    def test_extract_yaml_yaml_intermediate_not_dict_returns_none(self):
        """YAML traversal returns None when an intermediate key is not a mapping."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "data.yaml"
            # info is a string, not a dict → info.version should return None
            p.write_text("info: plain-string\n")
            result = _extract_yaml_field(p, "info.version")
            self.assertIsNone(result)

    def test_extract_yaml_yaml_key_not_in_nested_dict(self):
        """YAML traversal returns None when a nested key is absent."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "data.yaml"
            p.write_text("info:\n  name: svc\n")
            result = _extract_yaml_field(p, "info.version")
            self.assertIsNone(result)


class ExtractFieldFromBytesTests(unittest.TestCase):
    """Format dispatch and decoding behavior in _extract_field_from_bytes."""

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
        self.assertEqual(result, "1.0.0")

    def test_unsupported_extension_returns_none(self):
        raw = b"version=1.0.0\n"
        self.assertIsNone(self._fn(raw, "config.ini", "version"))

    def test_unicode_decode_error_returns_none(self):
        raw = b"\xff\xfe"  # Invalid UTF-8
        self.assertIsNone(self._fn(raw, "bad.json", "version"))


class ExtractJsonFromTextTests(unittest.TestCase):
    """JSON field extraction behavior in _extract_json_from_text."""

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

    def test_duplicate_keys_and_nonfinite_numbers_are_rejected(self):
        self.assertIsNone(
            self._fn('{"v":"1.0.0","v":"2.0.0"}', "v")
        )
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                self.assertIsNone(self._fn('{"v":' + value + "}", "v"))

    def test_oversized_integer_returns_none(self):
        self.assertIsNone(
            self._fn('{"v":' + "9" * 5000 + "}", "v")
        )

    def test_booleans_and_containers_are_not_version_identifiers(self):
        for value in ("true", "false", "null", "[]", "{}", "[1]", '{"x":1}'):
            with self.subTest(value=value):
                self.assertIsNone(self._fn('{"v":' + value + "}", "v"))

    @unittest.skipUnless(
        hasattr(sys, "set_int_max_str_digits"),
        "Python runtime has no configurable integer digit limit",
    )
    def test_bounded_integer_result_ignores_runtime_setting(self):
        digits = "9" * 1000
        original = sys.get_int_max_str_digits()
        try:
            results = []
            for setting in (640, 0):
                sys.set_int_max_str_digits(setting)
                results.append(self._fn('{"v":' + digits + "}", "v"))
        finally:
            sys.set_int_max_str_digits(original)

        self.assertEqual(results, [digits, digits])


class ExtractTomlFromTextTests(unittest.TestCase):
    """TOML field extraction behavior in _extract_toml_from_text."""

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

    def test_numeric_version_is_rejected_and_must_be_a_string(self):
        import boundver.versions as v_mod

        real_parser = v_mod.tomllib
        if real_parser is None:
            self.skipTest("tomllib/tomli is not installed")

        for parser in (real_parser, None):
            parser_name = getattr(parser, "__name__", "missing parser")
            with self.subTest(parser=parser_name):
                with patch.object(v_mod, "tomllib", parser):
                    for value in ("42", "-1", "9" * MAX_TOML_INTEGER_DIGITS):
                        with self.subTest(value_length=len(value)):
                            self.assertIsNone(
                                self._fn(f"version = {value}\n", "version")
                            )
                    expected = "8080" if parser is not None else None
                    self.assertEqual(
                        self._fn('version = "8080"\n', "version"), expected
                    )

    def test_unrelated_oversized_integer_is_rejected_before_parsing(self):
        oversized = "9" * (MAX_TOML_INTEGER_DIGITS + 1)
        text = f'build = {oversized}\nversion = "1.2.3"\n'
        self.assertIsNone(self._fn(text, "version"))

    def test_long_digits_in_strings_and_comments_do_not_trip_preflight(self):
        digits = "9" * (MAX_TOML_INTEGER_DIGITS + 1)
        text = (
            f'note = "{digits}"\n'
            f'multiline = """{digits}"""\n'
            f"# {digits}\n"
            'version = "1.2.3"\n'
        )
        self.assertEqual(self._fn(text, "version"), "1.2.3")

    def test_multiline_closing_quote_content_does_not_hide_later_number(self):
        oversized = "9" * (MAX_TOML_INTEGER_DIGITS + 1)
        text = (
            'note = """content""""\n'
            f"build = {oversized}\n"
            'version = "1.2.3"\n'
        )
        self.assertIsNone(self._fn(text, "version"))

    @unittest.skipUnless(
        hasattr(sys, "set_int_max_str_digits"),
        "Python runtime has no configurable integer digit limit",
    )
    def test_result_is_independent_of_runtime_digit_setting(self):
        selected_numeric = "version = " + "9" * 1000 + "\n"
        unrelated_numeric = (
            "build = " + "9" * 1000 + '\nversion = "1.2.3"\n'
        )
        original = sys.get_int_max_str_digits()
        try:
            results = []
            for setting in (640, 0):
                sys.set_int_max_str_digits(setting)
                results.append(
                    (
                        self._fn(selected_numeric, "version"),
                        self._fn(unrelated_numeric, "version"),
                    )
                )
        finally:
            sys.set_int_max_str_digits(original)

        self.assertEqual(results, [(None, None), (None, None)])

    def test_missing_parser_fails_closed_for_top_level_toml(self):
        import boundver.versions as v_mod
        orig = v_mod.tomllib
        v_mod.tomllib = None
        try:
            result = self._fn('version = "1.0.0"\n', "version")
            self.assertIsNone(result)
        finally:
            v_mod.tomllib = orig


class ExtractYamlFromTextTests(unittest.TestCase):
    """YAML field extraction behavior in _extract_yaml_from_text."""

    def setUp(self):
        from boundver.versions import _extract_yaml_from_text
        self._fn = _extract_yaml_from_text

    def test_top_level(self):
        result = self._fn("version: 1.0.0\n", "version")
        self.assertEqual(result, "1.0.0")

    def test_nested_via_yaml(self):
        result = self._fn("info:\n  version: 2.3.4\n", "info.version")
        self.assertEqual(result, "2.3.4")

    def test_missing_key_returns_none(self):
        result = self._fn("name: x\n", "version")
        self.assertIsNone(result)

    def test_oversized_implicit_and_explicit_integers_return_none(self):
        for tagged_value in ("9" * 4301, "!!int " + "9" * 4301):
            with self.subTest(explicit=tagged_value.startswith("!!int")):
                self.assertIsNone(
                    self._fn(f"version: {tagged_value}\n", "version")
                )

    def test_duplicate_keys_aliases_and_nonfinite_numbers_are_rejected(self):
        import boundver.versions as v_mod

        if v_mod.yaml is None:
            self.skipTest("PyYAML not installed")
        self.assertIsNone(
            self._fn("version: 1.0.0\nversion: 2.0.0\n", "version")
        )
        self.assertIsNone(
            self._fn("value: &value 1.0.0\nversion: *value\n", "version")
        )
        for value in (".nan", ".inf", "-.inf"):
            with self.subTest(value=value):
                self.assertIsNone(self._fn(f"version: {value}\n", "version"))

    def test_booleans_nulls_and_containers_are_not_version_identifiers(self):
        for value in ("true", "false", "null", "[]", "{}", "[1]", "{x: 1}"):
            with self.subTest(value=value):
                self.assertIsNone(
                    self._fn(f"version: {value}\n", "version")
                )

    @unittest.skipUnless(
        hasattr(sys, "set_int_max_str_digits"),
        "Python runtime has no configurable integer digit limit",
    )
    def test_large_integer_inside_container_is_always_rejected(self):
        digits = "9" * 1000
        original = sys.get_int_max_str_digits()
        try:
            results = []
            for setting in (640, 4300, 0):
                sys.set_int_max_str_digits(setting)
                results.append(
                    self._fn(f"version:\n  - {digits}\n", "version")
                )
        finally:
            sys.set_int_max_str_digits(original)
        self.assertEqual(results, [None, None, None])

    @unittest.skipUnless(
        hasattr(sys, "set_int_max_str_digits"),
        "Python runtime has no configurable integer digit limit",
    )
    def test_bounded_integer_result_ignores_runtime_setting(self):
        digits = "9" * 1000
        original = sys.get_int_max_str_digits()
        try:
            results = []
            for setting in (640, 0):
                sys.set_int_max_str_digits(setting)
                results.append(self._fn("version: " + digits + "\n", "version"))
        finally:
            sys.set_int_max_str_digits(original)

        self.assertEqual(results, [digits, digits])

    def test_yaml_exception_returns_none(self):
        """An authoritative parser failure returns no version."""
        with patch(
            "boundver.versions._load_yaml_with_bounded_integers",
            side_effect=ValueError("yaml exploded"),
        ):
            self.assertIsNone(self._fn("version: 1.2.3\n", "version"))

    def test_yaml_key_found_by_authoritative_parser(self):
        """A key found by PyYAML is returned unchanged."""
        import boundver.versions as v_mod
        if v_mod.yaml is None:
            self.skipTest("PyYAML not installed")
        result = self._fn("version: 5.6.7\n", "version")
        self.assertEqual(result, "5.6.7")

    def test_yaml_none_fails_closed_for_top_level_value(self):
        import boundver.versions as v_mod
        orig = v_mod.yaml
        v_mod.yaml = None
        try:
            result = self._fn("version: 0.9.1\n", "version")
            self.assertIsNone(result)
        finally:
            v_mod.yaml = orig


class ExtractFileVersionWithReadFnTests(unittest.TestCase):
    """extract_version behavior with an injected file reader."""

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
        """extract_version returns None when the injected reader raises OSError."""
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
        """extract_version returns None when the reader raises CalledProcessError."""
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

    def test_read_file_fn_rejects_non_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            result = self._fn(
                Path(td),
                "svc",
                version_source={"file": "package.json", "field": "version"},
                read_file_fn=MagicMock(return_value='{"version": "5.0.1"}'),
            )
        self.assertIsNone(result)

    def test_read_file_fn_enforces_the_version_file_limit(self):
        with tempfile.TemporaryDirectory() as td, patch(
            "boundver.versions.MAX_VERSION_FILE_BYTES", 4
        ):
            result = self._fn(
                Path(td),
                "svc",
                version_source={"file": "package.json", "field": "version"},
                read_file_fn=MagicMock(return_value=b"12345"),
            )
        self.assertIsNone(result)
