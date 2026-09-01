"""Contracts for the hosted docs and additional release channels."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest
import yaml


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
    validator = (ROOT / "scripts" / "validate_gitlab_component.py").read_text(
        encoding="utf-8"
    )
    assert ".read_bytes()" not in validator
    assert "MAX_COMPONENT_BYTES + 1" in validator
    component = (ROOT / "templates" / "boundver.yml").read_text(encoding="utf-8")
    assert "component: [version]" in component
    assert "ghcr.io/yzm1/boundver:$[[ component.version ]]" in component
    assert "git config --global" not in component
    assert "git rev-parse --is-shallow-repository" not in component
    assert "set -- boundver review" in component
    assert "--format plan" in component
    assert 'GIT_DEPTH: "$[[ inputs.history-depth ]]"' in component
    assert "History remediation: set GIT_DEPTH: 0" in component
    assert "boundver-result.json" in component
    assert "boundver-summary.md" in component
    assert "when: always" in component
    assert "trap ensure_boundver_artifacts EXIT" in component
    assert re.search(r"(?m)^\s+type: boolean$", component)

    documents = list(yaml.safe_load_all(component))
    inputs = documents[0]["spec"]["inputs"]
    assert inputs["operation"] == {
        "description": "Operation to run (verify or review).",
        "default": "verify",
        "options": ["verify", "review"],
    }
    assert inputs["base"]["default"] == ""
    assert inputs["target"]["default"] == ""
    assert inputs["merge-base"]["default"] is False
    assert inputs["history-depth"]["default"] == "20"
    job = next(iter(documents[1].values()))
    assert job["artifacts"] == {
        "name": "boundver-$CI_JOB_NAME-$CI_COMMIT_SHORT_SHA",
        "when": "always",
        "expire_in": "1 week",
        "paths": ["boundver-result.json", "boundver-summary.md"],
    }


def test_gitlab_catalog_release_requires_a_protected_semver_tag():
    pipeline = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    release_job = pipeline["publish-catalog-release"]
    condition = release_job["rules"][0]["if"]
    assert "CI_COMMIT_TAG" in condition
    assert "CI_COMMIT_REF_PROTECTED" in condition
    assert '== "true"' in condition
    assert pipeline["validate-component-source"]["timeout"] == "10m"
    assert release_job["timeout"] == "5m"


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
    assert "ghcr.io/aquasecurity/trivy@sha256:" in container
    assert "--ignorefile /policy/.trivyignore.yaml --exit-code 1" in container
    assert "--ignore-unfixed --exit-code 1" in container
    assert "push-to-registry: true" in container
    assert "oras cp --from-oci-layout" in container
    assert '"$archive@$ARCHIVE_DIGEST" "$IMAGE:$version"' in container
    assert 'DOCKER_CONFIG="$anonymous_config" docker pull' in container
    assert 'gh attestation verify "oci://$IMAGE@$DIGEST"' in container
