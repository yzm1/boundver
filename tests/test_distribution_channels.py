"""Contracts for the hosted docs and additional release channels."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"test_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


homebrew = _load("render_homebrew_formula")
gitlab = _load("validate_gitlab_component")


def test_homebrew_formula_is_exact_and_uses_the_standalone_release_asset():
    digest = "a" * 64
    formula = homebrew.render_formula("1.2.3", digest)
    assert "class Boundver < Formula" in formula
    assert "/releases/download/v1.2.3/boundver-1.2.3.pyz" in formula
    assert f'sha256 "{digest}"' in formula
    assert 'depends_on "python@3.14"' in formula
    assert 'python = formula_opt_bin("python@3.14")/"python3.14"' in formula
    assert "bin.mkpath" in formula
    assert '"--output", bin/"boundver", "--python", python' in formula
    assert 'assert_match "1.2.3"' in formula


@pytest.mark.parametrize(
    ("version", "digest"),
    (("v1.2.3", "a" * 64), ("1.2", "a" * 64), ("1.2.3", "A" * 64)),
)
def test_homebrew_formula_rejects_ambiguous_release_identity(version, digest):
    with pytest.raises(ValueError):
        homebrew.render_formula(version, digest)


def test_gitlab_component_is_version_bound_and_validated():
    assert gitlab.component_errors() == []
    component = (ROOT / "templates" / "boundver.yml").read_text(encoding="utf-8")
    assert "component: [version]" in component
    assert "ghcr.io/yzm1/boundver:$[[ component.version ]]" in component
    assert re.search(r"(?m)^\s+type: boolean$", component)


def test_docs_and_container_publish_from_pinned_dependencies():
    pages = (ROOT / ".github" / "workflows" / "docs.yml").read_text(
        encoding="utf-8"
    )
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    container = (
        ROOT / ".github" / "workflows" / "publish-container.yml"
    ).read_text(encoding="utf-8")
    assert "scripts/install_locked_tools.py docs" in pages
    assert "python -I -m mkdocs build --strict" in pages
    assert "org.opencontainers.image.source" in dockerfile
    assert "org.opencontainers.image.revision" in dockerfile
    assert "workflow_dispatch:" in container
    assert "workflow_call:" in container
    assert "linux/amd64,linux/arm64" in container
    assert "tonistiigi/binfmt:qemu-v10.2.3@sha256:" in container
    assert "version: v0.36.1" in container
    assert "image=moby/buildkit:v0.32.2@sha256:" in container
    assert "push-to-registry: true" in container
    assert "oras cp --from-oci-layout" in container
    assert 'DOCKER_CONFIG="$anonymous_config" docker pull' in container
    assert 'gh attestation verify "oci://$IMAGE@$DIGEST"' in container
