"""Supply-chain contracts for Python tooling used by repository automation."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "lock_release_tools.py"


def _load_locker():
    spec = importlib.util.spec_from_file_location("lock_release_tools", SCRIPT)
    if spec is None or spec.loader is None:  # pragma: no cover - import invariant
        raise AssertionError(f"cannot import {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_installer():
    path = ROOT / "scripts" / "install_locked_tools.py"
    spec = importlib.util.spec_from_file_location("install_locked_tools", path)
    if spec is None or spec.loader is None:  # pragma: no cover - import invariant
        raise AssertionError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _artifacts() -> dict[str, dict[str, object]]:
    payload = json.loads(
        (ROOT / "scripts" / "release-tool-artifacts.json").read_text(
            encoding="utf-8"
        )
    )
    return payload["packages"]


def _wheel_tags(filename: str) -> tuple[str, str, str]:
    stem = filename.removesuffix(".whl")
    _, python_tag, abi_tag, platform_tag = stem.rsplit("-", 3)
    return python_tag, abi_tag, platform_tag


def _supports_python(filename: str, minor: int) -> bool:
    python_tag, abi_tag, _ = _wheel_tags(filename)
    tags = python_tag.split(".")
    if "py3" in tags or f"py3{minor}" in tags or f"cp3{minor}" in tags:
        return True
    if abi_tag == "abi3":
        return any(
            tag.startswith("cp3") and tag[3:].isdigit() and int(tag[3:]) <= minor
            for tag in tags
        )
    return False


def _supports_platform(filename: str, platform: str) -> bool:
    _, _, platform_tag = _wheel_tags(filename)
    tags = platform_tag.split(".")
    if "any" in tags:
        return True
    if platform == "linux":
        return any(
            (tag.startswith("manylinux") or tag.startswith("linux"))
            and tag.endswith("x86_64")
            for tag in tags
        )
    if platform == "macos-intel":
        return any(
            tag.startswith("macosx")
            and (tag.endswith("x86_64") or tag.endswith("universal2"))
            for tag in tags
        )
    if platform == "macos-arm":
        return any(
            tag.startswith("macosx")
            and (tag.endswith("arm64") or tag.endswith("universal2"))
            for tag in tags
        )
    if platform == "windows":
        return "win_amd64" in tags
    raise AssertionError(f"unknown platform {platform}")


def test_generated_locks_are_canonical_and_complete():
    locker = _load_locker()
    locker.verify()

    _, profiles = locker.load_manifest()
    assert tuple(profiles) == ("action", "ci", "docs", "release")
    for profile in profiles.values():
        text = profile.lock.read_text(encoding="utf-8")
        assert text.count("--require-hashes") == 1
        assert text.count("--only-binary :all:") == 1
        for requirement in profile.requirements:
            assert f"{requirement.rendered} \\\n" in text
        hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})", text)
        assert hashes
        assert len(hashes) == len(set(hashes))


def test_lock_generation_rejects_active_pypi_advisories():
    locker = _load_locker()
    requirement = locker.Requirement("example", "1.0")
    payload = {
        "vulnerabilities": [
            {"id": "GHSA-aaaa-bbbb-cccc", "withdrawn": None},
            {"id": "PYSEC-2026-1", "withdrawn": "2026-01-01T00:00:00Z"},
        ]
    }

    assert locker._active_pypi_advisories(payload, requirement) == [
        "GHSA-aaaa-bbbb-cccc"
    ]
    with pytest.raises(locker.LockError, match="malformed vulnerability metadata"):
        locker._active_pypi_advisories(
            {"vulnerabilities": [{"id": "bad advisory", "withdrawn": None}]},
            requirement,
        )


def test_lock_writer_refuses_symlinks_and_preserves_regular_file_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    locker = _load_locker()
    root = tmp_path / "repo"
    output_dir = root / "scripts" / "requirements"
    output_dir.mkdir(parents=True)
    output = output_dir / "ci.lock"
    target = tmp_path / "outside.txt"
    target.write_bytes(b"outside")
    try:
        output.symlink_to(target)
    except OSError as exc:  # pragma: no cover - host policy dependent
        pytest.skip(f"symlinks unavailable: {exc}")
    monkeypatch.setattr(locker, "ROOT", root)

    with pytest.raises(locker.LockError, match="not a regular file"):
        locker._atomic_write(output, b"replacement")
    assert target.read_bytes() == b"outside"

    output.unlink()
    output.write_bytes(b"old")
    if os.name != "nt":
        output.chmod(0o640)
    locker._atomic_write(output, b"replacement")
    assert output.read_bytes() == b"replacement"
    if os.name != "nt":
        assert stat.S_IMODE(output.stat().st_mode) == 0o640


def test_action_native_wheels_cover_supported_python_and_os_matrix():
    artifacts = _artifacts()
    for package in ("pyyaml", "rpds-py", "tomli"):
        filenames = [wheel["filename"] for wheel in artifacts[package]["wheels"]]
        for minor in (10, 12):
            if package == "tomli" and minor == 12:
                continue
            for platform in ("linux", "macos-intel", "macos-arm", "windows"):
                assert any(
                    _supports_python(filename, minor)
                    and _supports_platform(filename, platform)
                    for filename in filenames
                ), f"{package} lacks a CPython 3.{minor} {platform} wheel"


def test_release_native_wheels_cover_python312_on_every_runner_os():
    artifacts = _artifacts()
    for package in (
        "cffi",
        "charset-normalizer",
        "cryptography",
        "nh3",
        "pydantic-core",
        "rfc3161-client",
    ):
        filenames = [wheel["filename"] for wheel in artifacts[package]["wheels"]]
        platforms = ("linux", "macos-arm", "windows") if package == "cryptography" else (
            "linux",
            "macos-intel",
            "macos-arm",
            "windows",
        )
        for platform in platforms:
            assert any(
                _supports_python(filename, 12)
                and _supports_platform(filename, platform)
                for filename in filenames
            ), f"{package} lacks a CPython 3.12 {platform} wheel"


def test_automation_uses_locked_or_offline_installs_only():
    automation = {
        relative: (ROOT / relative).read_text(encoding="utf-8")
        for relative in (
            "action.yml",
            ".github/workflows/ci.yml",
            ".github/workflows/create-release-tag.yml",
            ".github/workflows/publish.yml",
            "scripts/packaging_smoke.sh",
            "scripts/publish_release.py",
        )
    }
    combined = "\n".join(automation.values())
    assert "release-tool-constraints.txt" not in combined
    assert "--constraint" not in combined
    assert "pip install --upgrade" not in combined

    assert "scripts/install_locked_tools.py\" action" in automation["action.yml"]
    assert "scripts/install_locked_tools.py ci" in automation[
        ".github/workflows/ci.yml"
    ]
    for relative in (
        ".github/workflows/create-release-tag.yml",
        ".github/workflows/publish.yml",
        "scripts/packaging_smoke.sh",
    ):
        assert "scripts/install_locked_tools.py release" in automation[relative]

    installer = (ROOT / "scripts" / "install_locked_tools.py").read_text(
        encoding="utf-8"
    )
    for flag in (
        '"-I"',
        '"--isolated"',
        '"--no-cache-dir"',
        '"--require-hashes"',
        '"--only-binary=:all:"',
        '"https://pypi.org/simple"',
    ):
        assert flag in installer
    direct_lines = [line for line in combined.splitlines() if " -m pip " in line]
    assert direct_lines
    assert all("--isolated" in line for line in direct_lines)
    assert all(" -I -m pip " in line for line in direct_lines)
    assert '"--no-deps"' not in installer
    action = automation["action.yml"]
    assert 'python -I "${BOUNDVER_ACTION_PATH}/scripts/install_locked_tools.py"' in action
    assert "python -I -m boundver verify" in action
    assert 'python - "$result_file"' not in action
    for relative in (
        "action.yml",
        ".github/workflows/create-release-tag.yml",
        ".github/workflows/publish.yml",
        "scripts/packaging_smoke.sh",
    ):
        assert "--no-index" in automation[relative]
        assert "--no-deps" in automation[relative]


def test_locked_tool_install_has_a_process_deadline(monkeypatch, capsys):
    installer = _load_installer()

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(installer.subprocess, "run", timeout)
    assert installer.main(["ci"]) == 1
    captured = capsys.readouterr()
    assert "1800-second wall-clock limit" in captured.err


def test_locked_tool_install_resolves_the_hashed_dependency_set(monkeypatch):
    installer = _load_installer()
    observed = {}
    monkeypatch.setattr(installer.sys, "version_info", (3, 12, 0))

    def succeed(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(installer.subprocess, "run", succeed)

    assert installer.main(["release"]) == 0
    command = observed["command"]
    assert "--require-hashes" in command
    assert "--only-binary=:all:" in command
    assert "--no-deps" not in command
    assert observed["kwargs"]["stdin"] is subprocess.DEVNULL
    assert observed["kwargs"]["timeout"] == installer.MAX_INSTALL_SECONDS


def test_release_tool_install_rejects_unsupported_python(monkeypatch, capsys):
    installer = _load_installer()
    monkeypatch.setattr(installer.sys, "version_info", (3, 10, 18))
    run = monkeypatch.setattr(installer.subprocess, "run", pytest.fail)

    assert installer.main(["release"]) == 2
    assert run is None
    assert "release tools require Python 3.12 or newer" in capsys.readouterr().err
