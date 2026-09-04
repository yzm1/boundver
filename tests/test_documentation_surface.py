"""Keep the public documentation navigable, local, and editor-friendly."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterator

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
ROOT_SCHEMA = REPO_ROOT / "boundary.config.schema.json"
PACKAGED_SCHEMA = REPO_ROOT / "src" / "boundver" / "boundary.config.schema.json"


def _property_schemas(
    node: Any, path: str = "$"
) -> Iterator[tuple[str, dict[str, Any]]]:
    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict):
            for name, schema in properties.items():
                if isinstance(schema, dict):
                    property_path = f"{path}.{name}"
                    yield property_path, schema
                    yield from _property_schemas(schema, property_path)
        for key in ("additionalProperties", "items"):
            yield from _property_schemas(node.get(key), f"{path}.{key}")
        for key in ("oneOf", "anyOf", "allOf"):
            variants = node.get(key)
            if isinstance(variants, list):
                for index, variant in enumerate(variants):
                    yield from _property_schemas(
                        variant, f"{path}.{key}[{index}]"
                    )
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _property_schemas(item, f"{path}[{index}]")


def test_config_schemas_are_identical_valid_and_fully_described() -> None:
    assert ROOT_SCHEMA.read_bytes() == PACKAGED_SCHEMA.read_bytes()
    schema = json.loads(ROOT_SCHEMA.read_text(encoding="utf-8"))

    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft202012Validator.check_schema(schema)

    missing = [
        path
        for path, property_schema in _property_schemas(schema)
        if not property_schema.get("description")
    ]
    missing.extend(
        f"$.$defs.{name}"
        for name, definition in schema["$defs"].items()
        if not definition.get("description")
    )
    assert not missing, "schema fields without editor help:\n" + "\n".join(missing)

    defaults_help = schema["properties"]["defaults"]["description"]
    component_properties = schema["properties"]["components"][
        "additionalProperties"
    ]["properties"]
    assert "compat_mode applies to every component" in defaults_help
    assert "verify_facets is the fallback policy" in defaults_help
    assert "compat_mode" not in component_properties
    assert "verify_facets" in component_properties


def test_homepage_is_focused_responsive_and_private_by_default() -> None:
    mkdocs = (REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    index = (REPO_ROOT / "docs" / "index.md").read_text(encoding="utf-8")
    css = (
        REPO_ROOT / "docs" / "stylesheets" / "extra.css"
    ).read_text(encoding="utf-8")
    public_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            REPO_ROOT / "mkdocs.yml",
            REPO_ROOT / "docs" / "index.md",
            REPO_ROOT / "docs" / "overrides" / "main.html",
            REPO_ROOT / "docs" / "stylesheets" / "extra.css",
        )
    )

    assert "font: false" in mkdocs
    assert "extra_javascript:" not in mkdocs
    assert "fonts.googleapis.com" not in public_sources
    assert "fonts.gstatic.com" not in public_sources
    assert re.search(
        r"^hide:\s*\n\s+- navigation\s*\n\s+- toc\s*$",
        index,
        flags=re.MULTILINE,
    )
    assert len(re.findall(r"\]\([^)]*\)\{[^}]*\.md-button", index)) == 2
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in css
    assert ".md-typeset .bv-route-grid > ul" in css
    assert ".md-typeset ol.bv-steps:not([hidden])" in css
    assert "flex-wrap: wrap" in css
    assert ":focus-visible" in css
    assert "prefers-reduced-motion: reduce" in css
    assert "@media screen and (max-width: 44rem)" in css


def test_normative_docs_include_the_canonical_sources() -> None:
    mkdocs = (REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    workflow = (
        REPO_ROOT / ".github" / "workflows" / "docs.yml"
    ).read_text(encoding="utf-8")
    specification = (
        REPO_ROOT / "docs" / "specification.md"
    ).read_text(encoding="utf-8")
    hashing = (
        REPO_ROOT / "docs" / "hashing-contract.md"
    ).read_text(encoding="utf-8")
    support = (REPO_ROOT / "docs" / "support.md").read_text(encoding="utf-8")

    assert "pymdownx.snippets:" in mkdocs
    assert "restrict_base_path: true" in mkdocs
    assert "url_download: false" in mkdocs
    assert workflow.count('- "spec/**"') == 2
    assert workflow.count('- "SUPPORT.md"') == 2
    for wrapper, source in (
        (specification, "spec/spec.md"),
        (hashing, "spec/HASHING.md"),
        (support, "SUPPORT.md"),
    ):
        assert re.search(r"^hide:\s*\n\s+- edit\s*$", wrapper, re.MULTILINE)
        assert wrapper.rstrip().endswith(f'--8<-- "{source}"')


def test_release_review_allowlist_is_fully_documented() -> None:
    audit = (
        REPO_ROOT / "scripts" / "audit_release_reviews.sh"
    ).read_text(encoding="utf-8")
    releasing = (REPO_ROOT / "docs" / "RELEASING.md").read_text(
        encoding="utf-8"
    )
    assignments = {
        name: value
        for name, _, value in re.findall(
            r"(?m)^readonly codex_clean_(bang|period|question)_status_regex="
            r"(['\"])(.*?)\2$",
            audit,
        )
    }

    assert assignments.keys() == {"bang", "period", "question"}
    suffixes = {"bang": "!", "period": ".", "question": "?"}
    endings = {"bang": ")!", "period": r")\.", "question": r")\?"}
    for name, expression in assignments.items():
        assert expression.startswith("(")
        assert expression.endswith(endings[name])
        choices = expression[1 : -len(endings[name])]
        for choice in choices.split("|"):
            assert f"`{choice}{suffixes[name]}`" in releasing


def test_readme_keeps_the_decision_path_short() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    words = re.findall(r"\b[\w'-]+\b", readme)

    assert len(words) < 1800
    assert readme.index("## What it will not tell you") < readme.index(
        "## A practical configuration"
    )
    assert "A Git-aware lockfile and CI check" in readme
    assert "Semantic-provider implementation remains" in readme


def test_public_guidance_matches_component_facet_contracts() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    homepage = (REPO_ROOT / "docs" / "index.md").read_text(encoding="utf-8")
    executive_summary = (
        REPO_ROOT / "docs" / "executive-summary.md"
    ).read_text(encoding="utf-8")
    security_model = (
        REPO_ROOT / "docs" / "security-model.md"
    ).read_text(encoding="utf-8")
    troubleshooting = (
        REPO_ROOT / "docs" / "troubleshooting.md"
    ).read_text(encoding="utf-8")
    glossary = (REPO_ROOT / "docs" / "glossary.md").read_text(
        encoding="utf-8"
    )

    assert "tracked code under `src/`" in readme
    assert "boundver init --discover" in readme
    assert "tracked code under `src/`" in homepage
    assert "boundver init --discover" in homepage
    normalized_summary = " ".join(executive_summary.split())
    normalized_troubleshooting = " ".join(troubleshooting.split())
    normalized_glossary = " ".join(glossary.split())
    assert "records up to four identities for each component" in normalized_summary
    assert "implicit boundary without paths" in normalized_summary
    assert "`implicit` provider with one or more paths" in normalized_troubleshooting
    assert "non-`leaf`, non-`implicit` provider" in normalized_troubleshooting
    assert "An empty implicit boundary has no boundary digest" in normalized_troubleshooting
    assert "its digest includes the boundary digest" in normalized_glossary
    assert (
        "same content-only digest as the complete tracked tree under the component root"
        in normalized_glossary
    )
    assert "CRLF and LF line endings are equivalent" in normalized_glossary
    for public_facet_summary in (
        readme,
        homepage,
        executive_summary,
        glossary,
    ):
        assert "CRLF" in public_facet_summary
    assert "no unacknowledged gated facet drift remains" in readme
    assert "no unacknowledged gated drift remains" in readme
    assert "no unacknowledged gated drift remains" in homepage
    assert "acknowledged lock drift can still be present" in readme
    assert "acknowledged lock drift can still be present" in homepage
    assert "Omitting `facets` applies each component's configured" in readme
    assert "    facets: boundary,compat" not in readme
    assert "authoritative digest or\ncomparison fails closed" in security_model
    assert "complete: false" in security_model

    schema = json.loads(ROOT_SCHEMA.read_text(encoding="utf-8"))
    slice_mode_help = schema["properties"]["slices"]["additionalProperties"][
        "properties"
    ]["mode"]["description"]
    assert "--allow-partial" in slice_mode_help
    assert "null marker" in slice_mode_help
