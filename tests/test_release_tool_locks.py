"""Supply-chain contracts for Python tooling used by repository automation."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path


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
        for platform in ("linux", "macos-intel", "macos-arm", "windows"):
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
        '"--require-hashes"',
        '"--only-binary=:all:"',
        '"https://pypi.org/simple"',
    ):
        assert flag in installer

    direct_lines = [line for line in combined.splitlines() if " -m pip " in line]
    assert direct_lines
    assert all("--isolated" in line for line in direct_lines)
    assert all(" -I -m pip " in line for line in direct_lines)
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
