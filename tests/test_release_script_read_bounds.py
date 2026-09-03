"""Focused hard-bound contracts for local release helper inputs."""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
import types
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

import pytest

from tests._project_metadata import CURRENT_VERSION


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"bounded_{name}", path)
    if spec is None or spec.loader is None:  # pragma: no cover - import invariant
        raise AssertionError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


readiness = _load_script("verify_release_readiness")
changelog = _load_script("release_changelog")
standalone = _load_script("build_standalone")
probe = _load_script("probe_github_release")


def _underreported_stat(path: Path, size: int) -> types.SimpleNamespace:
    identity = path.stat()
    return types.SimpleNamespace(
        st_dev=identity.st_dev,
        st_ino=identity.st_ino,
        st_mode=identity.st_mode,
        st_size=size,
        st_mtime=identity.st_mtime,
        st_mtime_ns=identity.st_mtime_ns,
        st_atime_ns=identity.st_atime_ns,
        st_file_attributes=getattr(identity, "st_file_attributes", 0),
    )


def test_readiness_inventory_is_bounded_without_rglob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.py").write_text("a", encoding="utf-8")
    (tmp_path / "b.py").write_text("b", encoding="utf-8")

    def forbidden_rglob(*_args, **_kwargs):
        pytest.fail("release inventory must not call Path.rglob")

    monkeypatch.setattr(Path, "rglob", forbidden_rglob)
    assert [path.name for path in readiness._release_files(tmp_path)] == [
        "a.py",
        "b.py",
    ]

    monkeypatch.setattr(readiness, "MAX_RELEASE_TREE_ENTRIES", 1)
    with pytest.raises(readiness.ReleaseReadError, match="traversal limit"):
        list(readiness._release_files(tmp_path))


def test_readiness_read_uses_a_growth_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "surface.py"
    target.write_bytes(b"12345")
    underreported = _underreported_stat(target, 4)
    monkeypatch.setattr(readiness, "MAX_RELEASE_FILE_BYTES", 4)

    with mock.patch.object(
        readiness.Path, "lstat", return_value=underreported
    ), mock.patch.object(readiness.os, "fstat", return_value=underreported):
        with pytest.raises(readiness.ReleaseReadError, match="4-byte limit"):
            readiness._read_text(target)


def test_readiness_aggregate_and_diagnostic_retention_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_bytes(b"1234")
    second.write_bytes(b"x")
    monkeypatch.setattr(readiness, "MAX_RELEASE_TOTAL_BYTES", 4)
    budget = readiness._ReadBudget(tmp_path)
    assert budget.read_bytes(first) == b"1234"
    with pytest.raises(readiness.ReleaseReadError, match="aggregate limit"):
        budget.read_bytes(second)

    monkeypatch.setattr(readiness, "MAX_READINESS_DIAGNOSTICS", 3)
    monkeypatch.setattr(readiness, "MAX_READINESS_DIAGNOSTIC_CHARS", 32)
    monkeypatch.setattr(readiness, "MAX_READINESS_DIAGNOSTIC_TOTAL_CHARS", 96)
    diagnostics = readiness._Diagnostics()
    for index in range(100):
        diagnostics.append(f"diagnostic-{index}-" + ("x" * 100))
    assert len(diagnostics) <= 3
    assert sum(map(len, diagnostics)) <= 96
    assert "omitted" in diagnostics[-1]


def test_changelog_cli_does_not_parse_or_write_an_oversized_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "CHANGELOG.md"
    output = tmp_path / "notes.md"
    source.write_bytes(b"12345")
    monkeypatch.setattr(changelog, "MAX_CHANGELOG_BYTES", 4)

    with pytest.raises(SystemExit, match="4-byte limit"):
        changelog.main(
            [
                "--tag",
                "v1.2.3",
                "--changelog",
                str(source),
                "--output",
                str(output),
            ]
        )
    assert not output.exists()


def test_changelog_output_replaces_regular_files_but_refuses_symlinks(
    tmp_path: Path,
) -> None:
    regular = tmp_path / "notes.md"
    regular.write_text("old", encoding="utf-8")
    changelog._write_output_atomic(regular, "new\n")
    assert regular.read_text(encoding="utf-8") == "new\n"

    target = tmp_path / "outside.md"
    target.write_text("outside", encoding="utf-8")
    link = tmp_path / "linked-notes.md"
    try:
        link.symlink_to(target)
    except OSError as exc:  # pragma: no cover - host policy dependent
        pytest.skip(f"symlinks unavailable: {exc}")
    with pytest.raises(ValueError, match="not a regular file"):
        changelog._write_output_atomic(link, "replacement\n")
    assert target.read_text(encoding="utf-8") == "outside"


def test_standalone_preflight_bounds_metadata_and_lazy_tree_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "boundver"
    source.mkdir()
    (source / "a.py").write_text("a", encoding="utf-8")
    (source / "b.py").write_text("b", encoding="utf-8")
    monkeypatch.setattr(standalone, "MAX_SOURCE_TREE_ENTRIES", 1)
    with pytest.raises(standalone.StandaloneBuildError, match="traversal limit"):
        standalone._collect_source_tree(source)

    project = tmp_path / "pyproject.toml"
    project.write_text('[project]\nversion = "1.2.3"\n', encoding="utf-8")
    monkeypatch.setattr(standalone, "MAX_PROJECT_METADATA_BYTES", 8)
    with pytest.raises(standalone.StandaloneBuildError, match="8-byte limit"):
        standalone._project_version(project)


def test_standalone_preflight_rejects_symlinks_when_supported(
    tmp_path: Path,
) -> None:
    source = tmp_path / "boundver"
    source.mkdir()
    target = tmp_path / "outside.py"
    target.write_text("outside", encoding="utf-8")
    link = source / "linked.py"
    try:
        link.symlink_to(target)
    except OSError as exc:  # pragma: no cover - host policy dependent
        pytest.skip(f"symlinks unavailable: {exc}")
    with pytest.raises(standalone.StandaloneBuildError, match="symlink"):
        standalone._collect_source_tree(source)


def test_standalone_vendored_pyyaml_is_lock_bound_and_pure_python(
    tmp_path: Path,
) -> None:
    lock = tmp_path / "action.lock"
    lock.write_text("PyYAML==6.0.3 \\\n", encoding="utf-8")
    assert standalone._locked_pyyaml_version(lock) == "6.0.3"
    lock.write_text(
        "PyYAML==6.0.3 \\\nPyYAML==6.0.4 \\\n", encoding="utf-8"
    )
    with pytest.raises(standalone.StandaloneBuildError, match="exactly one"):
        standalone._locked_pyyaml_version(lock)

    package = tmp_path / "yaml"
    package.mkdir()
    for name in standalone._PYYAML_SOURCE_FILES:
        (package / name).write_text("# fixture\n", encoding="utf-8")
    (package / "_yaml.cp39-test.so").write_bytes(b"native")
    manifest = standalone._collect_pyyaml_tree(package)
    assert {entry.relative.as_posix() for entry in manifest.entries} == set(
        standalone._PYYAML_SOURCE_FILES
    )
    (package / "unexpected.py").write_text("# unexpected\n", encoding="utf-8")
    with pytest.raises(standalone.StandaloneBuildError, match="unexpected"):
        standalone._collect_pyyaml_tree(package)


@pytest.mark.parametrize(
    "member",
    ("../yaml/a.py", "/yaml/a.py", "C:/yaml/a.py", "yaml//a.py", "yaml/./a.py"),
)
def test_standalone_rejects_unsafe_distribution_paths(member: str) -> None:
    with pytest.raises(standalone.StandaloneBuildError, match="unsafe path"):
        standalone._distribution_member_name(member)


def test_bounded_standalone_archive_remains_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "boundver.pyz"
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    standalone.build(output)

    result = subprocess.run(
        [sys.executable, "-I", str(output), "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.rstrip().endswith(f" {CURRENT_VERSION}")


def test_probe_rejects_oversized_capture_before_workflow_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = tmp_path / "github-release-response.ABC123"
    response.write_bytes(b"12345")
    github_output = tmp_path / "github-output"
    monkeypatch.setattr(probe, "MAX_RESPONSE_BYTES", 4)
    stderr = io.StringIO()

    with redirect_stderr(stderr):
        result = probe.main(
            [
                "--api-exit",
                "0",
                "--response",
                str(response),
                "--runner-temp",
                str(tmp_path),
                "--github-output",
                str(github_output),
            ]
        )

    assert result == 1
    assert "4-byte limit" in stderr.getvalue()
    assert not github_output.exists()


def test_probe_response_read_uses_a_growth_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = tmp_path / "github-release-response.ABC123"
    response.write_bytes(b"12345")
    underreported = _underreported_stat(response, 4)
    monkeypatch.setattr(probe, "MAX_RESPONSE_BYTES", 4)

    with mock.patch.object(
        probe.Path, "lstat", return_value=underreported
    ), mock.patch.object(probe.os, "fstat", return_value=underreported):
        with pytest.raises(probe.ReleaseProbeError, match="4-byte limit"):
            probe._read_response(response)


@pytest.mark.skipif(
    not hasattr(sys, "set_int_max_str_digits"),
    reason="runtime has no mutable decimal conversion limit",
)
def test_release_json_integer_parsing_is_runtime_setting_independent() -> None:
    original = sys.get_int_max_str_digits()
    integer = "9" * 1_000
    oversized = "9" * (readiness.MAX_JSON_INTEGER_DIGITS + 1)
    try:
        for runtime_limit in (640, 0):
            sys.set_int_max_str_digits(runtime_limit)
            parsed = readiness._load_json(
                '{"$id":"schema","ignored":' + integer + "}"
            )
            assert parsed["$id"] == "schema"
            assert probe.parse_response(
                "HTTP/2 200 OK\r\n\r\n"
                '{"draft":false,"ignored":'
                + integer
                + "}"
            ) == (200, "public")
            with pytest.raises(ValueError, match="decimal-digit limit"):
                readiness._load_json('{"ignored":' + oversized + "}")
    finally:
        sys.set_int_max_str_digits(original)


@pytest.mark.skipif(
    not hasattr(sys, "set_int_max_str_digits"),
    reason="runtime has no mutable decimal conversion limit",
)
def test_readiness_toml_integer_preflight_is_runtime_setting_independent(
    tmp_path: Path,
) -> None:
    oversized = "9" * (readiness.MAX_TOML_INTEGER_DIGITS + 1)
    project = (
        '[project]\nname = "boundver"\nversion = "1.2.3"\nsequence = '
        + oversized
        + "\n"
    )
    (tmp_path / "pyproject.toml").write_text(project, encoding="utf-8")
    original = sys.get_int_max_str_digits()
    try:
        messages = []
        for runtime_limit in (640, 0):
            sys.set_int_max_str_digits(runtime_limit)
            errors = readiness.readiness_errors(tmp_path, "v1.2.3")
            messages.append(errors[0])
        assert messages[0] == messages[1]
        assert "cross-runtime limit" in messages[0]
    finally:
        sys.set_int_max_str_digits(original)

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "boundver"\nversion = "1.2.3"\nsequence = nan\n',
        encoding="utf-8",
    )
    assert "non-finite TOML" in readiness.readiness_errors(
        tmp_path, "v1.2.3"
    )[0]


def test_release_json_rejects_duplicates_and_nonfinite_numbers() -> None:
    for parser in (readiness._load_json, probe._load_json):
        with pytest.raises(ValueError, match="duplicate"):
            parser('{"draft":false,"draft":true}')
        for token in ("NaN", "Infinity", "-Infinity", "1e9999"):
            with pytest.raises(ValueError, match="non-finite"):
                parser('{"value":' + token + "}")


@pytest.mark.parametrize(
    "script_name",
    (
        "lock_release_tools",
        "publish_release",
        "verify_release_surfaces",
        "verify_testpypi_release",
    ),
)
def test_release_json_float_tokens_have_a_lexical_bound(script_name: str) -> None:
    module = _load_script(script_name)
    token = "1." + ("0" * (module.MAX_JSON_NUMBER_CHARS + 1))
    with pytest.raises(ValueError, match="character limit"):
        module._strict_json_loads('{"value":' + token + "}")


def test_readiness_json_shape_is_rejected_before_decoder_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(readiness, "MAX_JSON_TOKENS", 2)
    monkeypatch.setattr(
        readiness.json,
        "loads",
        lambda *_args, **_kwargs: pytest.fail("decoder must not run"),
    )
    with pytest.raises(ValueError, match="structural limit"):
        readiness._load_json("[0,0,0]")
