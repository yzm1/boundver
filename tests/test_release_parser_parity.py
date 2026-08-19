"""Cross-runtime parser contracts for release-control scripts."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    module_name = f"release_parser_parity_{name}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - import invariant
        raise AssertionError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


surfaces = _load_script("verify_release_surfaces")
testpypi = _load_script("verify_testpypi_release")
locker = _load_script("lock_release_tools")
publisher = _load_script("publish_release")

JSON_MODULES = (surfaces, testpypi, locker, publisher)
TOML_MODULES = (locker, publisher)


@pytest.mark.skipif(
    not hasattr(sys, "set_int_max_str_digits"),
    reason="runtime has no configurable integer conversion limit",
)
def test_json_integer_parsing_is_setting_independent_and_bounded() -> None:
    previous = sys.get_int_max_str_digits()
    accepted = "9" * 1000
    oversized = "9" * 4301
    try:
        for module in JSON_MODULES:
            errors: list[str] = []
            for setting in (640, 4300, 0):
                sys.set_int_max_str_digits(setting)
                payload = module._strict_json_loads(f'{{"ignored":{accepted}}}')
                assert type(payload["ignored"]) is int
                with pytest.raises(ValueError) as raised:
                    module._strict_json_loads(f'{{"ignored":{oversized}}}')
                errors.append(str(raised.value))
            assert errors == [
                "JSON integer exceeds the 4300-decimal-digit limit"
            ] * 3
    finally:
        sys.set_int_max_str_digits(previous)


@pytest.mark.parametrize(
    "document",
    (
        '{"value":1,"value":2}',
        '{"value":NaN}',
        '{"value":Infinity}',
        '{"value":-Infinity}',
        '{"value":1e9999}',
    ),
)
def test_release_json_parsers_reject_ambiguous_or_nonfinite_values(
    document: str,
) -> None:
    for module in JSON_MODULES:
        with pytest.raises(ValueError):
            module._strict_json_loads(document)


@pytest.mark.skipif(
    not hasattr(sys, "set_int_max_str_digits"),
    reason="runtime has no configurable integer conversion limit",
)
def test_toml_ignored_integer_is_rejected_identically_at_every_setting() -> None:
    previous = sys.get_int_max_str_digits()
    document = 'version = "1.2.3"\nignored = ' + ("9" * 1000) + "\n"
    try:
        for module in TOML_MODULES:
            errors: list[str] = []
            for setting in (640, 4300, 0):
                sys.set_int_max_str_digits(setting)
                with pytest.raises(ValueError) as raised:
                    module._strict_toml_loads(document)
                errors.append(str(raised.value))
            assert errors == [
                "TOML numeric token exceeds the "
                "640-digit cross-runtime safety limit"
            ] * 3
    finally:
        sys.set_int_max_str_digits(previous)


@pytest.mark.skipif(
    not hasattr(sys, "set_int_max_str_digits"),
    reason="runtime has no configurable integer conversion limit",
)
def test_toml_boundary_integer_is_accepted_at_every_setting() -> None:
    previous = sys.get_int_max_str_digits()
    document = "ignored = " + ("9" * 640) + "\n"
    try:
        for module in TOML_MODULES:
            for setting in (640, 4300, 0):
                sys.set_int_max_str_digits(setting)
                payload = module._strict_toml_loads(document)
                assert type(payload["ignored"]) is int
    finally:
        sys.set_int_max_str_digits(previous)


def test_toml_preflight_preserves_long_strings_comments_and_bare_keys() -> None:
    digits = "9" * 1000
    document = (
        f'payload = "{digits}"\n'
        f"# ignored = {digits}\n"
        f'service-{digits} = "ok"\n'
    )
    for module in TOML_MODULES:
        payload = module._strict_toml_loads(document)
        assert payload["payload"] == digits
        assert payload[f"service-{digits}"] == "ok"


def test_publish_project_version_remains_string_only(tmp_path: Path) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[project]\nname = "boundver"\nversion = 12\n', encoding="utf-8"
    )
    with pytest.raises(publisher.GateError, match="name/version does not match"):
        publisher._project(tmp_path, "v0.0.12")


@pytest.mark.skipif(
    not hasattr(sys, "set_int_max_str_digits"),
    reason="runtime has no configurable integer conversion limit",
)
def test_parsed_github_ids_and_identity_diagnostics_are_setting_independent() -> None:
    previous = sys.get_int_max_str_digits()
    digits = "9" * 1000
    try:
        testpypi_messages: list[str] = []
        publisher_messages: list[str] = []
        for setting in (640, 4300, 0):
            sys.set_int_max_str_digits(setting)
            release = testpypi._strict_json_loads(
                f'{{"info":{{"name":{digits},"version":"1.2.3"}},"urls":[]}}'
            )
            with pytest.raises(testpypi.ReleaseVerificationError) as raised:
                testpypi._parse_remote_release(
                    release, "boundver", "1.2.3", "https://example.invalid"
                )
            testpypi_messages.append(str(raised.value))

            summary = publisher._strict_json_loads(
                f'{{"id":{digits},"tag_name":"v1.2.3"}}'
            )
            with (
                mock.patch.object(
                    publisher, "_gh_paginated_list", return_value=[summary]
                ),
                pytest.raises(publisher.GateError) as raised,
            ):
                publisher._github_release_for_tag(Path("."), "v1.2.3")
            publisher_messages.append(str(raised.value))
        assert len(set(testpypi_messages)) == 1
        assert len(set(publisher_messages)) == 1
    finally:
        sys.set_int_max_str_digits(previous)
