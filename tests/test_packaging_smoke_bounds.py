"""Focused bounds for the inline packaging-smoke inspectors."""

from __future__ import annotations

import io
import struct
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Iterable, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "packaging_smoke.sh"
PROJECT_START = "expected_version=$(python -I - <<'PY' | head -c 129\n"
PROJECT_END = "\nPY\n)\n"
ARCHIVE_START = (
    'python -I - "$wheel_path" "$sdist_path" "$pyz_path" '
    '"$expected_version" "$PWD/LICENSE" "$smoke_root" '
    '"$PWD/scripts/requirements/action.lock" <<\'PY\'\n'
)
ARCHIVE_END = "\nPY\n\nwheel_venv="
VERSION = "1.2.3"
LICENSE_BYTES = b"bounded test license\n"
SCRIPT_SOURCE = SCRIPT.read_text(encoding="utf-8")


def _extract_program(start_marker: str, end_marker: str) -> str:
    start = SCRIPT_SOURCE.index(start_marker) + len(start_marker)
    end = SCRIPT_SOURCE.index(end_marker, start)
    return SCRIPT_SOURCE[start:end]


PROJECT_PROGRAM = _extract_program(PROJECT_START, PROJECT_END)
ARCHIVE_PROGRAM = _extract_program(ARCHIVE_START, ARCHIVE_END)


def _run_project(
    directory: Path, *, int_limit: Optional[int] = None
) -> subprocess.CompletedProcess[str]:
    program = PROJECT_PROGRAM
    if int_limit is not None:
        program = (
            f"import sys\nsys.set_int_max_str_digits({int_limit})\n" + program
        )
    return subprocess.run(
        [sys.executable, "-I", "-"],
        cwd=directory,
        input=program,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _write_zip(
    path: Path,
    entries: list[tuple[str, bytes]],
    *,
    prefix: bytes = b"",
) -> None:
    if prefix:
        path.write_bytes(prefix)
        mode = "a"
    else:
        mode = "w"
    with zipfile.ZipFile(path, mode) as archive:
        for name, payload in entries:
            archive.writestr(name, payload)


def _write_sdist(path: Path, entries: Iterable[tuple[str, bytes]]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, payload in entries:
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))


def _artifacts(directory: Path) -> tuple[Path, Path, Path, Path, Path]:
    wheel = directory / "boundver-1.2.3-py3-none-any.whl"
    sdist = directory / "boundver-1.2.3.tar.gz"
    pyz = directory / "boundver.pyz"
    license_path = directory / "LICENSE"
    scratch = directory / "scratch"
    license_path.write_bytes(LICENSE_BYTES)
    scratch.mkdir()
    metadata = (
        b"Metadata-Version: 2.1\n"
        b"Name: boundver\n"
        b"Version: 1.2.3\n\n"
    )
    _write_zip(
        wheel,
        [
            ("boundver/__init__.py", b""),
            ("boundver/__main__.py", b""),
            ("boundver/boundary.config.schema.json", b"{}"),
            ("boundver/py.typed", b""),
            ("boundver-1.2.3.dist-info/METADATA", metadata),
            ("boundver-1.2.3.dist-info/licenses/LICENSE", LICENSE_BYTES),
        ],
    )
    prefix = "boundver-1.2.3"
    _write_sdist(
        sdist,
        [
            (f"{prefix}/CODE_OF_CONDUCT.md", b""),
            (f"{prefix}/SECURITY.md", b""),
            (f"{prefix}/SUPPORT.md", b""),
            (f"{prefix}/docs/getting-started.md", b""),
            (f"{prefix}/spec/HASHING.md", b""),
            (f"{prefix}/spec/cli-output.plan.schema.json", b"{}"),
            (f"{prefix}/spec/cli-output.review.schema.json", b"{}"),
            (f"{prefix}/spec/cli-output.slice.schema.json", b"{}"),
            (f"{prefix}/spec/cli-output.why.schema.json", b"{}"),
        ],
    )
    _write_zip(
        pyz,
        [
            ("LICENSE", LICENSE_BYTES),
            (
                "boundver-1.2.3.dist-info/METADATA",
                b"Metadata-Version: 2.4\n"
                b"Name: boundver\n"
                b"Version: 1.2.3\n"
                b"License-File: licenses/LICENSE\n"
                b"License-File: licenses/PyYAML-LICENSE\n\n",
            ),
            ("boundver-1.2.3.dist-info/VENDORED", b"PyYAML==6.0.3\n"),
            ("boundver-1.2.3.dist-info/licenses/LICENSE", LICENSE_BYTES),
            (
                "boundver-1.2.3.dist-info/licenses/PyYAML-LICENSE",
                b"PyYAML test license\n",
            ),
        ]
        + [
            (f"yaml/{name}", b"")
            for name in (
                "__init__.py",
                "composer.py",
                "constructor.py",
                "cyaml.py",
                "dumper.py",
                "emitter.py",
                "error.py",
                "events.py",
                "loader.py",
                "nodes.py",
                "parser.py",
                "reader.py",
                "representer.py",
                "resolver.py",
                "scanner.py",
                "serializer.py",
                "tokens.py",
            )
        ],
        prefix=b"#!/usr/bin/env python3\n",
    )
    return wheel, sdist, pyz, license_path, scratch


def _run_archive(
    wheel: Path,
    sdist: Path,
    pyz: Path,
    license_path: Path,
    scratch: Path,
    *,
    cwd: Optional[Path] = None,
) -> subprocess.CompletedProcess[str]:
    action_lock = license_path.parent / "action.lock"
    action_lock.write_text("PyYAML==6.0.3 \\\n", encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-",
            str(wheel),
            str(sdist),
            str(pyz),
            VERSION,
            str(license_path),
            str(scratch),
            str(action_lock),
        ],
        cwd=license_path.parent if cwd is None else cwd,
        input=ARCHIVE_PROGRAM,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _patch_zip_uncompressed_sizes(path: Path, size: int) -> None:
    content = bytearray(path.read_bytes())
    eocd = content.rfind(b"PK\x05\x06")
    assert eocd >= 0
    total_entries = struct.unpack_from("<H", content, eocd + 10)[0]
    cursor = struct.unpack_from("<L", content, eocd + 16)[0]
    for _ in range(total_entries):
        assert content[cursor : cursor + 4] == b"PK\x01\x02"
        struct.pack_into("<L", content, cursor + 24, size)
        name_size, extra_size, comment_size = struct.unpack_from(
            "<3H", content, cursor + 28
        )
        cursor += 46 + name_size + extra_size + comment_size
    path.write_bytes(content)


def test_inline_inspectors_compile_and_keep_sentinel_identity_contracts():
    compile(PROJECT_PROGRAM, "packaging-smoke-project", "exec")
    compile(ARCHIVE_PROGRAM, "packaging-smoke-archives", "exec")
    assert "remaining + 1" in PROJECT_PROGRAM
    assert "changed(opened, finished)" in PROJECT_PROGRAM
    assert "changed(finished, current)" in PROJECT_PROGRAM
    assert "plain_directory(parent" in PROJECT_PROGRAM
    assert "remaining + 1" in ARCHIVE_PROGRAM
    assert "MAX_ARCHIVE_TOTAL_NAME_BYTES" in ARCHIVE_PROGRAM
    assert "MAX_ARCHIVE_TOTAL_BYTES" in ARCHIVE_PROGRAM
    assert "MAX_ARCHIVE_SOURCE_TOTAL_BYTES" in ARCHIVE_PROGRAM
    assert "changed(opened, finished)" in ARCHIVE_PROGRAM
    assert "changed(finished, current)" in ARCHIVE_PROGRAM
    assert "parent_initial = plain_directory" in ARCHIVE_PROGRAM
    assert "repository LICENSE path escapes" in ARCHIVE_PROGRAM


def test_shell_output_capture_is_fixed_or_avoided():
    assert SCRIPT_SOURCE.count("$(") == 2
    assert "expected_version=$(python -I - <<'PY' | head -c 129" in SCRIPT_SOURCE
    assert "MAX_VERSION_BYTES = 128" in PROJECT_PROGRAM
    assert 'mktemp -d "${TMPDIR:-/tmp}/bv-pkg.XXXXXXXXXX"' in (
        SCRIPT_SOURCE
    )
    assert "head -c 4097" in SCRIPT_SOURCE
    assert "${#smoke_root} -gt 4096" in SCRIPT_SOURCE
    assert '"$smoke_leaf" != bv-pkg.*' in SCRIPT_SOURCE
    assert 'wheel_venv="$smoke_root/w"' in SCRIPT_SOURCE
    assert 'sdist_venv="$smoke_root/s"' in SCRIPT_SOURCE
    assert 'standalone_venv="$smoke_root/a"' in SCRIPT_SOURCE
    assert "wheel_python=$resolved_venv_python" in SCRIPT_SOURCE
    assert "wheel_python=$(" not in SCRIPT_SOURCE


def test_project_version_command_output_is_bounded(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nversion = "1.2.3"\n', encoding="utf-8")

    accepted = _run_project(tmp_path)

    assert accepted.returncode == 0, accepted.stderr
    assert accepted.stdout == "1.2.3\n"

    pyproject.write_text(
        '[project]\nversion = "' + ("v" * 129) + '"\n',
        encoding="utf-8",
    )
    rejected_output = _run_project(tmp_path)
    assert rejected_output.returncode != 0
    assert rejected_output.stdout == ""
    assert "output limit" in rejected_output.stderr


def test_project_reader_rejects_oversized_file_before_parse(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    with pyproject.open("wb") as stream:
        stream.seek(1024 * 1024)
        stream.write(b"x")

    result = _run_project(tmp_path)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "1048576-byte limit" in result.stderr


def test_project_numeric_token_limit_is_runtime_setting_independent(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    oversized_integer = "7" * 1000
    pyproject.write_text(
        '[project]\nversion = "1.2.3"\nignored = ' + oversized_integer + "\n",
        encoding="utf-8",
    )
    limits = (640, 4300, 0) if hasattr(sys, "set_int_max_str_digits") else (None,)

    results = [_run_project(tmp_path, int_limit=limit) for limit in limits]

    outcomes = {
        (result.returncode, result.stdout, result.stderr) for result in results
    }
    assert len(outcomes) == 1
    result = results[0]
    assert result.returncode != 0
    assert result.stdout == ""
    assert "640-decimal-digit limit" in result.stderr

    pyproject.write_text(
        '[project]\nversion = "1.2.3"\nignored = "'
        + oversized_integer
        + '"\n',
        encoding="utf-8",
    )
    string_result = _run_project(
        tmp_path,
        int_limit=640 if hasattr(sys, "set_int_max_str_digits") else None,
    )
    assert string_result.returncode == 0, string_result.stderr
    assert string_result.stdout == "1.2.3\n"


def test_archive_inspector_accepts_bounded_release_artifacts(tmp_path):
    result = _run_archive(*_artifacts(tmp_path))

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_archive_inspector_rejects_oversized_license(tmp_path):
    wheel, sdist, pyz, license_path, scratch = _artifacts(tmp_path)
    with license_path.open("wb") as stream:
        stream.seek(4 * 1024 * 1024)
        stream.write(b"x")

    result = _run_archive(wheel, sdist, pyz, license_path, scratch)

    assert result.returncode != 0
    assert "repository LICENSE exceeds the 4194304-byte limit" in result.stderr


def test_archive_license_must_be_contained_in_working_tree_root(tmp_path):
    repository = tmp_path / "repo"
    outside = tmp_path / "outside"
    repository.mkdir()
    outside.mkdir()
    wheel, sdist, pyz, license_path, scratch = _artifacts(repository)

    result = _run_archive(
        wheel,
        sdist,
        pyz,
        license_path,
        scratch,
        cwd=outside,
    )

    assert result.returncode != 0
    assert "LICENSE path escapes the working tree root" in result.stderr


def test_zip_member_count_is_preflighted_from_bounded_trailer(tmp_path):
    wheel, sdist, pyz, license_path, scratch = _artifacts(tmp_path)
    content = bytearray(wheel.read_bytes())
    eocd = content.rfind(b"PK\x05\x06")
    assert eocd >= 0
    struct.pack_into("<HH", content, eocd + 8, 10_001, 10_001)
    wheel.write_bytes(content)

    result = _run_archive(wheel, sdist, pyz, license_path, scratch)

    assert result.returncode != 0
    assert "10000-member limit" in result.stderr


def test_zip_member_paths_are_bounded_before_inventory(tmp_path):
    wheel, sdist, pyz, license_path, scratch = _artifacts(tmp_path)
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("x" * 4097, b"")

    result = _run_archive(wheel, sdist, pyz, license_path, scratch)

    assert result.returncode != 0
    assert "overlong ZIP member name" in result.stderr


def test_zip_member_aggregate_is_preflighted_before_decompression(tmp_path):
    wheel, sdist, pyz, license_path, scratch = _artifacts(tmp_path)
    _patch_zip_uncompressed_sizes(wheel, 100 * 1024 * 1024)

    result = _run_archive(wheel, sdist, pyz, license_path, scratch)

    assert result.returncode != 0
    assert "uncompressed aggregate limit" in result.stderr


def test_tar_member_count_is_preflighted_before_tarfile_inventory(tmp_path):
    wheel, sdist, pyz, license_path, scratch = _artifacts(tmp_path)
    _write_sdist(
        sdist,
        ((f"boundver-1.2.3/{index}", b"") for index in range(10_001)),
    )

    result = _run_archive(wheel, sdist, pyz, license_path, scratch)

    assert result.returncode != 0
    assert "10000-member limit" in result.stderr


def test_tar_member_paths_are_bounded_before_semantic_checks(tmp_path):
    wheel, sdist, pyz, license_path, scratch = _artifacts(tmp_path)
    _write_sdist(sdist, [("x" * 4097, b"")])

    result = _run_archive(wheel, sdist, pyz, license_path, scratch)

    assert result.returncode != 0
    assert "unsafe or overlong member name" in result.stderr


def test_archive_source_size_is_rejected_before_archive_parsing(tmp_path):
    wheel = tmp_path / "oversized.whl"
    with wheel.open("wb") as stream:
        stream.seek(256 * 1024 * 1024)
        stream.write(b"x")
    sdist = tmp_path / "unused.tar.gz"
    pyz = tmp_path / "unused.pyz"
    sdist.touch()
    pyz.touch()
    license_path = tmp_path / "LICENSE"
    license_path.write_bytes(LICENSE_BYTES)
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    result = _run_archive(wheel, sdist, pyz, license_path, scratch)

    assert result.returncode != 0
    assert "wheel exceeds the 268435456-byte limit" in result.stderr


def test_archive_source_aggregate_is_rejected_before_archive_parsing(tmp_path):
    sources = []
    for name in ("large.whl", "large.tar.gz", "large.pyz"):
        path = tmp_path / name
        with path.open("wb") as stream:
            stream.seek(180 * 1024 * 1024)
            stream.write(b"x")
        sources.append(path)
    license_path = tmp_path / "LICENSE"
    license_path.write_bytes(LICENSE_BYTES)
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    result = _run_archive(
        sources[0], sources[1], sources[2], license_path, scratch
    )

    assert result.returncode != 0
    assert "aggregate source limit" in result.stderr
