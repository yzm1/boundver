"""Focused, hermetic tests for the post-release public-surface verifier."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from tests._project_metadata import (
    CURRENT_MINOR_TAG,
    CURRENT_TAG,
    CURRENT_VERSION,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
TAG = CURRENT_TAG
VERSION = CURRENT_VERSION
SHA = "a" * 40
ALIAS = CURRENT_MINOR_TAG
RELEASE_NOTES = "### Changed\n\n- Shipped the exact verified candidate.\n"


def _load_verifier():
    path = REPO_ROOT / "scripts" / "verify_release_surfaces.py"
    spec = importlib.util.spec_from_file_location("release_surface_verifier", path)
    if spec is None or spec.loader is None:  # pragma: no cover - import invariant
        raise AssertionError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


verifier = _load_verifier()


class FakeFetcher:
    def __init__(self, routes: dict[str, Any]):
        self.routes = routes
        self.requests: list[tuple[str, str]] = []

    def __call__(self, url: str, accept: str):
        self.requests.append((url, accept))
        if url not in self.routes:
            raise AssertionError(f"unexpected request: {url}")
        value = self.routes[url]
        if callable(value):
            value = value()
        if isinstance(value, verifier.HttpResponse):
            return value
        if isinstance(value, str):
            value = value.encode("utf-8")
        return verifier.HttpResponse(value, url)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


@pytest.fixture
def candidate(tmp_path: Path):
    dist = tmp_path / "dist"
    assets = tmp_path / "release-assets"
    dist.mkdir()
    assets.mkdir()
    payloads = {
        f"boundver-{VERSION}-py3-none-any.whl": b"exact wheel bytes\n",
        f"boundver-{VERSION}.tar.gz": b"exact sdist bytes\n",
        f"boundver-{VERSION}.pyz": b"#!/usr/bin/env python3\nexact zipapp bytes\n",
    }
    for name, payload in payloads.items():
        directory = dist if name.endswith((".whl", ".tar.gz")) else assets
        (directory / name).write_bytes(payload)
    manifest = "".join(
        f"{_sha(payloads[name])}  {name}\n" for name in sorted(payloads)
    ).encode("utf-8")
    (assets / "SHA256SUMS").write_bytes(manifest)
    payloads["SHA256SUMS"] = manifest
    (tmp_path / "release-notes.md").write_text(RELEASE_NOTES, encoding="utf-8")
    return dist, assets, payloads


def _registry_payload(
    registry: verifier.Registry, payloads: dict[str, bytes]
) -> tuple[dict[str, Any], dict[str, bytes]]:
    urls: list[dict[str, Any]] = []
    downloads: dict[str, bytes] = {}
    for name in sorted(
        item for item in payloads if item.endswith((".whl", ".tar.gz"))
    ):
        url = f"{registry.file_origin}/packages/{name}"
        content = payloads[name]
        urls.append(
            {
                "filename": name,
                "size": len(content),
                "digests": {"sha256": _sha(content)},
                "url": url,
                "yanked": False,
                "packagetype": "bdist_wheel" if name.endswith(".whl") else "sdist",
            }
        )
        downloads[url] = content
    payload = {
        "info": {
            "name": "boundver",
            "version": VERSION,
            "summary": verifier.SUMMARY,
            "requires_python": verifier.REQUIRES_PYTHON,
            "project_urls": dict(verifier.REQUIRED_PROJECT_URLS),
        },
        "urls": urls,
    }
    return payload, downloads


def _marketplace_html(tag: str = TAG) -> str:
    embedded = {
        "payload": {
            "action": {"slug": "boundver"},
            "repository": {"owner": "yzm1", "name": "boundver"},
            "releaseData": {
                "latestRelease": {"tagName": tag, "isPrerelease": False}
            },
        }
    }
    return (
        "<!doctype html><html><body>"
        '<script type="application/json" '
        'data-target="react-app.embeddedData">'
        f"{json.dumps(embedded)}"
        "</script></body></html>"
    )


def _surface_routes(payloads: dict[str, bytes]) -> dict[str, Any]:
    routes: dict[str, Any] = {}
    for registry in verifier.REGISTRIES:
        payload, downloads = _registry_payload(registry, payloads)
        routes[
            f"{registry.api_origin}/pypi/boundver/{VERSION}/json"
        ] = json.dumps(payload).encode("utf-8")
        routes.update(downloads)

    assets = []
    for name, content in sorted(payloads.items()):
        assets.append(
            {
                "name": name,
                "size": len(content),
                "digest": f"sha256:{_sha(content)}",
                "state": "uploaded",
                "browser_download_url": (
                    f"https://github.com/yzm1/boundver/releases/download/{TAG}/{name}"
                ),
            }
        )
    routes[
        f"https://api.github.com/repos/yzm1/boundver/releases/tags/{TAG}"
    ] = json.dumps(
        {
            "tag_name": TAG,
            "target_commitish": "main",
            "name": f"boundver {VERSION}",
            "body": RELEASE_NOTES,
            "draft": False,
            "prerelease": False,
            "immutable": True,
            "published_at": "2026-08-12T12:00:00Z",
            "assets": assets,
        }
    ).encode("utf-8")
    routes[
        f"https://api.github.com/repos/yzm1/boundver/git/ref/tags/{TAG}"
    ] = json.dumps(
        {"ref": f"refs/tags/{TAG}", "object": {"type": "commit", "sha": SHA}}
    ).encode("utf-8")
    routes[
        f"https://api.github.com/repos/yzm1/boundver/git/ref/tags/{ALIAS}"
    ] = json.dumps(
        {"ref": f"refs/tags/{ALIAS}", "object": {"type": "commit", "sha": SHA}}
    ).encode("utf-8")
    routes[
        "https://github.com/marketplace/actions/boundver"
    ] = _marketplace_html()
    return routes


def _verify(candidate, fetch: FakeFetcher, **overrides: Any) -> None:
    dist, assets, _ = candidate
    arguments = {
        "tag": TAG,
        "sha": SHA,
        "alias": ALIAS,
        "dist_dir": dist,
        "release_notes": dist.parent / "release-notes.md",
        "release_assets_dir": assets,
        "fetch": fetch,
    }
    arguments.update(overrides)
    verifier.verify_release_surfaces(**arguments)


def test_complete_phase_verifies_all_surfaces(candidate):
    routes = _surface_routes(candidate[2])
    fetch = FakeFetcher(routes)

    _verify(candidate, fetch)

    requested = {url for url, _ in fetch.requests}
    assert f"https://pypi.org/pypi/boundver/{VERSION}/json" in requested
    assert f"https://test.pypi.org/pypi/boundver/{VERSION}/json" in requested
    assert (
        f"https://api.github.com/repos/yzm1/boundver/git/ref/tags/{TAG}" in requested
    )
    assert (
        f"https://api.github.com/repos/yzm1/boundver/git/ref/tags/{ALIAS}" in requested
    )
    assert "https://github.com/marketplace/actions/boundver" in requested


def test_release_assets_directory_may_contain_exact_distribution_copies(candidate):
    dist, assets, payloads = candidate
    for path in dist.iterdir():
        assets.joinpath(path.name).write_bytes(path.read_bytes())

    _verify(candidate, FakeFetcher(_surface_routes(payloads)))

    wheel = assets / f"boundver-{VERSION}-py3-none-any.whl"
    wheel.write_bytes(b"not the promoted wheel")
    with pytest.raises(verifier.ReleaseVerificationError, match="copy disagrees"):
        _verify(candidate, FakeFetcher({}))


def test_marketplace_phase_defers_production_pypi_and_alias(candidate):
    routes = _surface_routes(candidate[2])
    routes.pop(f"https://pypi.org/pypi/boundver/{VERSION}/json")
    routes.pop(
        f"https://api.github.com/repos/yzm1/boundver/git/ref/tags/{ALIAS}"
    )
    fetch = FakeFetcher(routes)

    _verify(candidate, fetch, phase="marketplace")

    requested = {url for url, _ in fetch.requests}
    assert not any(url.startswith("https://pypi.org/") for url in requested)
    assert (
        f"https://api.github.com/repos/yzm1/boundver/git/ref/tags/{ALIAS}"
        not in requested
    )
    assert f"https://test.pypi.org/pypi/boundver/{VERSION}/json" in requested


def test_github_phase_verifies_only_the_immutable_release(candidate):
    routes = _surface_routes(candidate[2])
    fetch = FakeFetcher(routes)

    _verify(candidate, fetch, phase="github")

    requested = {url for url, _ in fetch.requests}
    assert not any("pypi.org" in url for url in requested)
    assert "https://github.com/marketplace/actions/boundver" not in requested
    assert (
        f"https://api.github.com/repos/yzm1/boundver/git/ref/tags/{ALIAS}"
        not in requested
    )
    assert (
        f"https://api.github.com/repos/yzm1/boundver/releases/tags/{TAG}"
        in requested
    )
    assert (
        f"https://api.github.com/repos/yzm1/boundver/git/ref/tags/{TAG}"
        in requested
    )


def test_complete_phase_accepts_alias_none_only_when_alias_is_skipped(candidate):
    routes = _surface_routes(candidate[2])
    routes.pop(
        f"https://api.github.com/repos/yzm1/boundver/git/ref/tags/{ALIAS}"
    )
    fetch = FakeFetcher(routes)

    _verify(
        candidate,
        fetch,
        phase="complete",
        alias="none",
        verify_alias=False,
    )

    requested = {url for url, _ in fetch.requests}
    assert f"https://pypi.org/pypi/boundver/{VERSION}/json" in requested
    assert f"https://test.pypi.org/pypi/boundver/{VERSION}/json" in requested
    assert (
        f"https://api.github.com/repos/yzm1/boundver/git/ref/tags/{ALIAS}"
        not in requested
    )


def test_alias_none_is_rejected_before_network_without_skip_alias(candidate):
    fetch = FakeFetcher({})

    with pytest.raises(verifier.ReleaseVerificationError, match="requires --skip-alias"):
        _verify(candidate, fetch, alias="none")

    assert fetch.requests == []


def test_cli_accepts_alias_none_with_skip_alias(candidate, monkeypatch):
    fetch = FakeFetcher(_surface_routes(candidate[2]))
    monkeypatch.setattr(verifier, "_stdlib_fetch", fetch)

    assert verifier.main(
        [
            "--tag",
            TAG,
            "--sha",
            SHA,
            "--alias",
            "none",
            "--skip-alias",
            "--dist-dir",
            str(candidate[0]),
            "--release-assets-dir",
            str(candidate[1]),
            "--release-notes",
            str(candidate[0].parent / "release-notes.md"),
            "--attempts",
            "1",
        ]
    ) == 0

    alias_url = f"https://api.github.com/repos/yzm1/boundver/git/ref/tags/{ALIAS}"
    assert alias_url not in {url for url, _ in fetch.requests}


def test_cli_rejects_alias_none_without_skip_alias(candidate, monkeypatch, capsys):
    fetch = FakeFetcher({})
    monkeypatch.setattr(verifier, "_stdlib_fetch", fetch)

    assert verifier.main(
        [
            "--tag",
            TAG,
            "--sha",
            SHA,
            "--alias",
            "none",
            "--dist-dir",
            str(candidate[0]),
            "--release-assets-dir",
            str(candidate[1]),
            "--release-notes",
            str(candidate[0].parent / "release-notes.md"),
            "--attempts",
            "1",
        ]
    ) == 1

    assert "requires --skip-alias" in capsys.readouterr().err
    assert fetch.requests == []


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("summary", "wrong", "summary"),
        ("requires_python", ">=3.10", "Requires-Python"),
        ("project_urls", {}, "project URL"),
    ],
)
def test_registry_metadata_must_match_release_contract(
    candidate, field: str, value: Any, message: str
):
    routes = _surface_routes(candidate[2])
    url = f"https://pypi.org/pypi/boundver/{VERSION}/json"
    payload = json.loads(routes[url])
    payload["info"][field] = value
    routes[url] = json.dumps(payload).encode("utf-8")

    with pytest.raises(verifier.ReleaseVerificationError, match=message):
        _verify(candidate, FakeFetcher(routes))


def test_registry_downloaded_bytes_must_equal_local_candidate(candidate):
    routes = _surface_routes(candidate[2])
    wheel_url = (
        "https://files.pythonhosted.org/packages/"
        f"boundver-{VERSION}-py3-none-any.whl"
    )
    routes[wheel_url] = b"different bytes with an untrusted digest"

    with pytest.raises(verifier.ReleaseVerificationError, match="downloaded PyPI bytes"):
        _verify(candidate, FakeFetcher(routes))


def test_registry_download_cannot_redirect_to_another_origin(candidate):
    routes = _surface_routes(candidate[2])
    wheel_url = (
        "https://files.pythonhosted.org/packages/"
        f"boundver-{VERSION}-py3-none-any.whl"
    )
    routes[wheel_url] = verifier.HttpResponse(
        candidate[2][f"boundver-{VERSION}-py3-none-any.whl"],
        "https://attacker.invalid/wheel.whl",
    )

    with pytest.raises(verifier.ReleaseVerificationError, match="escaped"):
        _verify(candidate, FakeFetcher(routes))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("draft", True, "draft"),
        ("prerelease", True, "prerelease"),
        ("immutable", False, "not immutable"),
        ("target_commitish", "", "non-empty string"),
    ],
)
def test_github_release_must_be_public_stable_immutable_and_exact(
    candidate, field: str, value: Any, message: str
):
    routes = _surface_routes(candidate[2])
    url = f"https://api.github.com/repos/yzm1/boundver/releases/tags/{TAG}"
    payload = json.loads(routes[url])
    payload[field] = value
    routes[url] = json.dumps(payload).encode("utf-8")

    with pytest.raises(verifier.ReleaseVerificationError, match=message):
        _verify(candidate, FakeFetcher(routes))


def test_github_asset_names_and_digests_are_exact(candidate):
    routes = _surface_routes(candidate[2])
    url = f"https://api.github.com/repos/yzm1/boundver/releases/tags/{TAG}"
    payload = json.loads(routes[url])
    payload["assets"][0]["digest"] = f"sha256:{'f' * 64}"
    routes[url] = json.dumps(payload).encode("utf-8")

    with pytest.raises(verifier.ReleaseVerificationError, match="asset disagrees"):
        _verify(candidate, FakeFetcher(routes))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("name", f"boundver {CURRENT_TAG}", "title disagrees"),
        ("body", RELEASE_NOTES.replace("exact ", ""), "notes disagree"),
    ],
)
def test_github_release_title_and_notes_are_exact(
    candidate, field: str, value: str, message: str
):
    routes = _surface_routes(candidate[2])
    url = f"https://api.github.com/repos/yzm1/boundver/releases/tags/{TAG}"
    payload = json.loads(routes[url])
    payload[field] = value
    routes[url] = json.dumps(payload).encode("utf-8")

    with pytest.raises(verifier.ReleaseVerificationError, match=message):
        _verify(candidate, FakeFetcher(routes))


def test_existing_tag_release_may_report_default_branch_target_commitish(candidate):
    routes = _surface_routes(candidate[2])

    _verify(candidate, FakeFetcher(routes))


def test_github_may_omit_only_the_release_notes_final_newline(candidate):
    routes = _surface_routes(candidate[2])
    url = f"https://api.github.com/repos/yzm1/boundver/releases/tags/{TAG}"
    payload = json.loads(routes[url])
    payload["body"] = RELEASE_NOTES.rstrip("\n")
    routes[url] = json.dumps(payload).encode("utf-8")

    _verify(candidate, FakeFetcher(routes))


@pytest.mark.parametrize("newline", ["\r\n", "\r"])
def test_github_release_notes_accept_transport_newline_spelling(candidate, newline):
    routes = _surface_routes(candidate[2])
    url = f"https://api.github.com/repos/yzm1/boundver/releases/tags/{TAG}"
    payload = json.loads(routes[url])
    payload["body"] = RELEASE_NOTES.replace("\n", newline)
    routes[url] = json.dumps(payload).encode("utf-8")

    _verify(candidate, FakeFetcher(routes))


def test_annotated_release_tag_is_dereferenced_to_exact_commit(candidate):
    routes = _surface_routes(candidate[2])
    ref_url = f"https://api.github.com/repos/yzm1/boundver/git/ref/tags/{TAG}"
    tag_object_sha = "c" * 40
    routes[ref_url] = json.dumps(
        {
            "ref": f"refs/tags/{TAG}",
            "object": {"type": "tag", "sha": tag_object_sha},
        }
    ).encode("utf-8")
    routes[
        f"https://api.github.com/repos/yzm1/boundver/git/tags/{tag_object_sha}"
    ] = json.dumps(
        {"sha": tag_object_sha, "object": {"type": "commit", "sha": SHA}}
    ).encode("utf-8")

    _verify(candidate, FakeFetcher(routes))


def test_tag_and_explicit_minor_alias_must_resolve_to_exact_sha(candidate):
    routes = _surface_routes(candidate[2])
    alias_url = (
        f"https://api.github.com/repos/yzm1/boundver/git/ref/tags/{ALIAS}"
    )
    routes[alias_url] = json.dumps(
        {
            "ref": f"refs/tags/{ALIAS}",
            "object": {"type": "commit", "sha": "b" * 40},
        }
    ).encode("utf-8")

    with pytest.raises(verifier.ReleaseVerificationError, match="compatibility alias"):
        _verify(candidate, FakeFetcher(routes))

    fetch = FakeFetcher(routes)
    with pytest.raises(verifier.ReleaseVerificationError, match="does not match release line"):
        _verify(candidate, fetch, alias="v0.10")
    assert fetch.requests == []


def test_marketplace_embedded_latest_release_must_match_exact_tag(candidate):
    routes = _surface_routes(candidate[2])
    routes["https://github.com/marketplace/actions/boundver"] = _marketplace_html(
        "v0.10.0"
    )

    with pytest.raises(verifier.ReleaseVerificationError, match="latest stable"):
        _verify(candidate, FakeFetcher(routes))


def test_checksum_manifest_is_verified_before_network(candidate):
    candidate[1].joinpath("SHA256SUMS").write_text(
        f"{'0' * 64}  boundver-{VERSION}.pyz\n", encoding="utf-8"
    )
    fetch = FakeFetcher({})

    with pytest.raises(verifier.ReleaseVerificationError, match="one newline-terminated"):
        _verify(candidate, fetch)
    assert fetch.requests == []


def test_release_notes_are_validated_before_network(candidate):
    notes = candidate[0].parent / "release-notes.md"
    notes.write_text(RELEASE_NOTES.rstrip("\n"), encoding="utf-8")
    fetch = FakeFetcher({})

    with pytest.raises(verifier.ReleaseVerificationError, match="ending with a newline"):
        _verify(candidate, fetch)
    assert fetch.requests == []


def test_retries_stale_public_state_with_injected_sleep(candidate):
    routes = _surface_routes(candidate[2])
    marketplace_url = "https://github.com/marketplace/actions/boundver"
    pages = [_marketplace_html("v0.10.0"), _marketplace_html(TAG)]
    routes[marketplace_url] = lambda: pages.pop(0)
    fetch = FakeFetcher(routes)
    sleeps: list[float] = []
    notices: list[tuple[int, int, str]] = []

    verifier.verify_release_surfaces_with_retries(
        tag=TAG,
        sha=SHA,
        alias=ALIAS,
        dist_dir=candidate[0],
        release_notes=candidate[0].parent / "release-notes.md",
        release_assets_dir=candidate[1],
        attempts=2,
        delay_seconds=0.25,
        fetch=fetch,
        sleep=sleeps.append,
        retry_notice=lambda attempt, attempts, error: notices.append(
            (attempt, attempts, str(error))
        ),
    )

    assert sleeps == [0.25]
    assert notices == [(1, 2, f"Marketplace does not report {TAG} as the latest stable release")]
    assert sum(url == marketplace_url for url, _ in fetch.requests) == 2


def test_retries_remain_bounded(candidate):
    routes = _surface_routes(candidate[2])
    marketplace_url = "https://github.com/marketplace/actions/boundver"
    routes[marketplace_url] = _marketplace_html("v0.10.0")
    fetch = FakeFetcher(routes)
    sleeps: list[float] = []

    with pytest.raises(verifier.ReleaseVerificationError, match="latest stable"):
        verifier.verify_release_surfaces_with_retries(
            tag=TAG,
            sha=SHA,
            alias=ALIAS,
            dist_dir=candidate[0],
            release_notes=candidate[0].parent / "release-notes.md",
            release_assets_dir=candidate[1],
            attempts=3,
            delay_seconds=0,
            fetch=fetch,
            sleep=sleeps.append,
        )

    assert sleeps == [0, 0]
    assert sum(url == marketplace_url for url, _ in fetch.requests) == 3


class _StreamingResponse(io.BytesIO):
    def __init__(self, payload: bytes, url: str, headers: dict[str, str]):
        super().__init__(payload)
        self.url = url
        self.headers = headers
        self.status = 200
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return super().read(size)

    def geturl(self) -> str:
        return self.url


def test_stdlib_fetch_rejects_oversized_content_length_before_read(monkeypatch):
    url = "https://api.github.com/example"
    response = _StreamingResponse(
        b"{}",
        url,
        {"Content-Length": str(verifier.MAX_PUBLIC_DOCUMENT_BYTES + 1)},
    )
    monkeypatch.setattr(verifier.urllib.request, "urlopen", lambda *args, **kwargs: response)

    with pytest.raises(verifier.ReleaseNetworkError, match="response limit"):
        verifier._stdlib_fetch(url, "application/json")

    assert response.read_sizes == []


def test_stdlib_fetch_uses_one_byte_growth_sentinel(monkeypatch):
    url = "https://api.github.com/example"
    response = _StreamingResponse(b"abcde-more", url, {})
    monkeypatch.setattr(verifier, "MAX_PUBLIC_DOCUMENT_BYTES", 4)
    monkeypatch.setattr(verifier.urllib.request, "urlopen", lambda *args, **kwargs: response)

    with pytest.raises(verifier.ReleaseNetworkError, match="4-byte response limit"):
        verifier._stdlib_fetch(url, "application/json")

    assert response.read_sizes == [4, 1]


def test_local_artifact_size_is_rejected_before_open(tmp_path, monkeypatch):
    artifact = tmp_path / "oversized.whl"
    artifact.write_bytes(b"ab")
    monkeypatch.setattr(verifier, "MAX_RELEASE_ARTIFACT_BYTES", 1)

    def fail_open(*args, **kwargs):
        raise AssertionError("oversized artifact must not be opened")

    monkeypatch.setattr(Path, "open", fail_open)
    with pytest.raises(verifier.ReleaseVerificationError, match="1-byte limit"):
        verifier._artifact(artifact)
