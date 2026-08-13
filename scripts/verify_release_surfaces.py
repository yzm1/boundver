#!/usr/bin/env python3
"""Verify that every public boundver release surface agrees exactly.

This is a post-release, fail-closed check.  It uses only the Python standard
library and deliberately performs anonymous requests: a release that is only
visible with maintainer credentials is not a public release.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


PROJECT = "boundver"
SUMMARY = "Detect API boundary, behavioral contract, and component changes in CI"
REQUIRES_PYTHON = ">=3.9"
DEFAULT_REPOSITORY = "yzm1/boundver"
DEFAULT_MARKETPLACE_SLUG = "boundver"
USER_AGENT = "boundver-release-surface-verifier/1"
TAG_RE = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
ALIAS_RE = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)")
SHA_RE = re.compile(r"[0-9a-f]{40}")
DIGEST_RE = re.compile(r"[0-9a-f]{64}")
REPOSITORY_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/"
    r"[A-Za-z0-9_.-]+"
)
SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
CHECKSUM_LINE_RE = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._+-]*)")
PHASES = frozenset({"marketplace", "complete"})
REQUIRED_PROJECT_URLS = {
    "Homepage": "https://github.com/yzm1/boundver",
    "Documentation": "https://github.com/yzm1/boundver/tree/main/docs",
    "Changelog": "https://github.com/yzm1/boundver/blob/main/CHANGELOG.md",
    "Issues": "https://github.com/yzm1/boundver/issues",
    "Repository": "https://github.com/yzm1/boundver",
    "GitHub Action": "https://github.com/marketplace/actions/boundver",
}


class ReleaseVerificationError(ValueError):
    """A public surface is malformed or disagrees with the release candidate."""


class ReleaseNetworkError(RuntimeError):
    """A public surface could not be read."""


@dataclass(frozen=True)
class HttpResponse:
    """Small injectable HTTP response used by the verifier and its tests."""

    body: bytes
    final_url: str
    status: int = 200


Fetcher = Callable[[str, str], HttpResponse]
Sleeper = Callable[[float], None]
RetryNotice = Callable[[int, int, Exception], None]


@dataclass(frozen=True)
class LocalArtifact:
    name: str
    path: Path
    size: int
    sha256: str


@dataclass(frozen=True)
class Registry:
    name: str
    api_origin: str
    file_origin: str


REGISTRIES = (
    Registry("PyPI", "https://pypi.org", "https://files.pythonhosted.org"),
    Registry(
        "TestPyPI",
        "https://test.pypi.org",
        "https://test-files.pythonhosted.org",
    ),
)


def _stdlib_fetch(url: str, accept: str) -> HttpResponse:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return HttpResponse(
                body=response.read(),
                final_url=response.geturl(),
                status=response.status,
            )
    except urllib.error.HTTPError as error:
        raise ReleaseNetworkError(
            f"public surface returned HTTP {error.code}: {url}"
        ) from error
    except (OSError, urllib.error.URLError) as error:
        raise ReleaseNetworkError(f"cannot read public surface {url}: {error}") from error


def _origin(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ReleaseVerificationError(f"unsafe public URL: {url!r}")
    return f"{parsed.scheme}://{parsed.netloc}"


def _request(
    fetch: Fetcher,
    url: str,
    accept: str,
    *,
    allowed_origins: frozenset[str],
) -> bytes:
    if _origin(url) not in allowed_origins:
        raise ReleaseVerificationError(f"request URL has an unexpected origin: {url!r}")
    try:
        response = fetch(url, accept)
    except (ReleaseNetworkError, ReleaseVerificationError):
        raise
    except Exception as error:
        raise ReleaseNetworkError(f"cannot read public surface {url}: {error}") from error
    if not isinstance(response, HttpResponse):
        raise ReleaseVerificationError("fetcher must return HttpResponse")
    if response.status != 200:
        raise ReleaseNetworkError(
            f"public surface returned HTTP {response.status}: {url}"
        )
    if _origin(response.final_url) not in allowed_origins:
        raise ReleaseVerificationError(
            f"public request escaped its expected origin: {response.final_url!r}"
        )
    return response.body


def _request_json(
    fetch: Fetcher, url: str, *, allowed_origins: frozenset[str]
) -> Mapping[str, Any]:
    payload = _request(
        fetch,
        url,
        "application/vnd.github+json, application/json",
        allowed_origins=allowed_origins,
    )
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseVerificationError(f"public API returned invalid JSON: {url}") from error
    if not isinstance(value, dict):
        raise ReleaseVerificationError(f"public API response must be an object: {url}")
    return value


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _artifact(path: Path) -> LocalArtifact:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise ReleaseVerificationError(f"cannot read local artifact {path}: {error}") from error
    return LocalArtifact(path.name, path, len(payload), _sha256_bytes(payload))


def _directory_entries(path: Path) -> set[str]:
    if not path.is_dir():
        raise ReleaseVerificationError(f"artifact directory is missing: {path}")
    entries: set[str] = set()
    try:
        children = list(path.iterdir())
    except OSError as error:
        raise ReleaseVerificationError(
            f"cannot inspect artifact directory {path}: {error}"
        ) from error
    for child in children:
        if child.is_symlink() or not child.is_file():
            raise ReleaseVerificationError(
                f"artifact directory may contain only regular files: {child}"
            )
        entries.add(child.name)
    return entries


def _load_local_artifacts(
    dist_dir: Path, release_assets_dir: Path, version: str
) -> tuple[dict[str, LocalArtifact], dict[str, LocalArtifact]]:
    dist_dir = dist_dir.resolve()
    release_assets_dir = release_assets_dir.resolve()
    dist_entries = _directory_entries(dist_dir)
    wheel_prefix = f"{PROJECT}-{version}-"
    wheels = sorted(
        name
        for name in dist_entries
        if name.startswith(wheel_prefix) and name.endswith(".whl")
    )
    expected_sdist = f"{PROJECT}-{version}.tar.gz"
    if len(wheels) != 1 or expected_sdist not in dist_entries:
        raise ReleaseVerificationError(
            "distribution directory must contain one version-matched wheel and sdist"
        )
    dist_names = {wheels[0], expected_sdist}

    pyz_name = f"{PROJECT}-{version}.pyz"
    checksum_name = "SHA256SUMS"
    asset_names = {pyz_name, checksum_name}
    asset_entries = _directory_entries(release_assets_dir)
    if dist_dir == release_assets_dir:
        expected_entries = dist_names | asset_names
        if dist_entries != expected_entries:
            raise ReleaseVerificationError(
                "combined artifact directory must contain exactly the wheel, sdist, "
                f"{pyz_name}, and {checksum_name}"
            )
    else:
        if dist_entries != dist_names:
            raise ReleaseVerificationError(
                "distribution directory must contain exactly one wheel and one sdist"
            )
        if asset_entries not in (asset_names, asset_names | dist_names):
            raise ReleaseVerificationError(
                "release-assets directory must contain exactly the standalone and "
                "checksum manifest, optionally with exact copies of the wheel and sdist"
            )

    distributions = {
        name: _artifact(dist_dir / name) for name in sorted(dist_names)
    }
    release_assets = (
        {
            name: _artifact(release_assets_dir / name)
            for name in sorted(dist_names)
        }
        if dist_names <= asset_entries
        else dict(distributions)
    )
    for name, distribution in distributions.items():
        release_copy = release_assets[name]
        if (
            release_copy.size != distribution.size
            or release_copy.sha256 != distribution.sha256
        ):
            raise ReleaseVerificationError(
                f"release-assets copy disagrees with the Python distribution: {name}"
            )
    release_assets[pyz_name] = _artifact(release_assets_dir / pyz_name)
    release_assets[checksum_name] = _artifact(release_assets_dir / checksum_name)
    _verify_checksum_manifest(
        release_assets_dir / checksum_name,
        {name: artifact for name, artifact in release_assets.items() if name != checksum_name},
    )
    return distributions, release_assets


def _verify_checksum_manifest(
    path: Path, artifacts: Mapping[str, LocalArtifact]
) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ReleaseVerificationError(f"cannot read {path.name}: {error}") from error
    lines = text.splitlines()
    if not text.endswith("\n") or len(lines) != len(artifacts):
        raise ReleaseVerificationError(
            f"{path.name} must contain exactly one newline-terminated entry per binary artifact"
        )
    found: dict[str, str] = {}
    for line in lines:
        match = CHECKSUM_LINE_RE.fullmatch(line)
        if match is None or match.group(2) in found:
            raise ReleaseVerificationError(f"malformed or duplicate entry in {path.name}")
        found[match.group(2)] = match.group(1)
    if set(found) != set(artifacts):
        raise ReleaseVerificationError(
            f"{path.name} filenames do not match the release binary artifacts"
        )
    for name, artifact in artifacts.items():
        if found[name] != artifact.sha256:
            raise ReleaseVerificationError(
                f"{path.name} digest does not match local artifact: {name}"
            )


def _load_release_notes(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ReleaseVerificationError(
            f"release notes must be a regular file: {path}"
        )
    try:
        notes = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ReleaseVerificationError(
            f"cannot read release notes {path}: {error}"
        ) from error
    if not notes.strip() or not notes.endswith("\n"):
        raise ReleaseVerificationError(
            "release notes must be non-empty UTF-8 text ending with a newline"
        )
    return notes


def _safe_remote_filename(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or Path(value).name != value
        or "/" in value
        or "\\" in value
    ):
        raise ReleaseVerificationError("registry supplied an unsafe filename")
    return value


def _registry_release_url(registry: Registry, version: str) -> str:
    project = urllib.parse.quote(PROJECT, safe="")
    release = urllib.parse.quote(version, safe="")
    return f"{registry.api_origin}/pypi/{project}/{release}/json"


def _validate_registry_file_url(
    registry: Registry, url: object, filename: str
) -> str:
    if not isinstance(url, str):
        raise ReleaseVerificationError(f"{registry.name} supplied a non-string file URL")
    parsed = urllib.parse.urlsplit(url)
    if (
        _origin(url) != registry.file_origin
        or parsed.query
        or parsed.fragment
        or urllib.parse.unquote(Path(parsed.path).name) != filename
    ):
        raise ReleaseVerificationError(
            f"{registry.name} supplied an unsafe URL for {filename}"
        )
    return url


def _verify_registry(
    fetch: Fetcher,
    registry: Registry,
    version: str,
    local: Mapping[str, LocalArtifact],
) -> None:
    payload = _request_json(
        fetch,
        _registry_release_url(registry, version),
        allowed_origins=frozenset({registry.api_origin}),
    )
    info = payload.get("info")
    urls = payload.get("urls")
    if not isinstance(info, dict) or not isinstance(urls, list):
        raise ReleaseVerificationError(
            f"{registry.name} response is missing info or urls"
        )
    if info.get("name") != PROJECT or info.get("version") != version:
        raise ReleaseVerificationError(f"{registry.name} release identity disagrees")
    if info.get("summary") != SUMMARY:
        raise ReleaseVerificationError(f"{registry.name} project summary disagrees")
    if info.get("requires_python") != REQUIRES_PYTHON:
        raise ReleaseVerificationError(
            f"{registry.name} Requires-Python must be {REQUIRES_PYTHON!r}"
        )
    project_urls = info.get("project_urls")
    if not isinstance(project_urls, dict):
        raise ReleaseVerificationError(
            f"{registry.name} project_urls must be an object"
        )
    for label, expected in REQUIRED_PROJECT_URLS.items():
        if project_urls.get(label) != expected:
            raise ReleaseVerificationError(
                f"{registry.name} project URL {label!r} disagrees"
            )

    remote: dict[str, tuple[int, str, str]] = {}
    for item in urls:
        if not isinstance(item, dict):
            raise ReleaseVerificationError(f"{registry.name} urls entries must be objects")
        filename = _safe_remote_filename(item.get("filename"))
        if filename in remote:
            raise ReleaseVerificationError(
                f"{registry.name} supplied duplicate metadata for {filename}"
            )
        size = item.get("size")
        digests = item.get("digests")
        digest = digests.get("sha256") if isinstance(digests, dict) else None
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or DIGEST_RE.fullmatch(digest) is None
            or item.get("yanked") is not False
        ):
            raise ReleaseVerificationError(
                f"{registry.name} supplied malformed metadata for {filename}"
            )
        expected_type = "bdist_wheel" if filename.endswith(".whl") else "sdist"
        if item.get("packagetype") != expected_type:
            raise ReleaseVerificationError(
                f"{registry.name} supplied the wrong package type for {filename}"
            )
        url = _validate_registry_file_url(registry, item.get("url"), filename)
        remote[filename] = (size, digest, url)

    if set(remote) != set(local):
        raise ReleaseVerificationError(
            f"{registry.name} filenames do not exactly match the local candidate"
        )
    for filename, artifact in local.items():
        size, digest, url = remote[filename]
        if size != artifact.size or digest != artifact.sha256:
            raise ReleaseVerificationError(
                f"{registry.name} metadata disagrees with local artifact: {filename}"
            )
        downloaded = _request(
            fetch,
            url,
            "application/octet-stream",
            allowed_origins=frozenset({registry.file_origin}),
        )
        try:
            local_bytes = artifact.path.read_bytes()
        except OSError as error:  # pragma: no cover - already read during discovery
            raise ReleaseVerificationError(
                f"cannot reread local artifact {artifact.path}: {error}"
            ) from error
        if downloaded != local_bytes:
            raise ReleaseVerificationError(
                f"downloaded {registry.name} bytes disagree with local artifact: {filename}"
            )


def _github_api_url(repository: str, suffix: str) -> str:
    owner, name = repository.split("/", 1)
    return (
        "https://api.github.com/repos/"
        f"{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(name, safe='')}/{suffix}"
    )


def _verify_tag_ref(
    fetch: Fetcher, repository: str, tag: str, expected_sha: str, label: str
) -> None:
    """Resolve one exact Git tag, including annotated tags, to a commit SHA."""
    reference = _request_json(
        fetch,
        _github_api_url(
            repository, f"git/ref/tags/{urllib.parse.quote(tag, safe='')}"
        ),
        allowed_origins=frozenset({"https://api.github.com"}),
    )
    expected_ref = f"refs/tags/{tag}"
    if reference.get("ref") != expected_ref:
        raise ReleaseVerificationError(
            f"GitHub {label} response does not identify {expected_ref}"
        )
    target = reference.get("object")
    seen: set[str] = set()
    for _ in range(8):
        if not isinstance(target, dict):
            raise ReleaseVerificationError(f"GitHub {label} has no Git object")
        object_type = target.get("type")
        object_sha = target.get("sha")
        if not isinstance(object_sha, str) or SHA_RE.fullmatch(object_sha) is None:
            raise ReleaseVerificationError(f"GitHub {label} has an invalid Git object")
        if object_type == "commit":
            if object_sha != expected_sha:
                raise ReleaseVerificationError(
                    f"GitHub {label} does not resolve to {expected_sha}"
                )
            return
        if object_type != "tag" or object_sha in seen:
            raise ReleaseVerificationError(
                f"GitHub {label} does not resolve to a commit"
            )
        seen.add(object_sha)
        annotated = _request_json(
            fetch,
            _github_api_url(repository, f"git/tags/{object_sha}"),
            allowed_origins=frozenset({"https://api.github.com"}),
        )
        if annotated.get("sha") != object_sha:
            raise ReleaseVerificationError(
                f"GitHub {label} annotated-tag identity disagrees"
            )
        target = annotated.get("object")
    raise ReleaseVerificationError(f"GitHub {label} annotation chain is too deep")


def _validate_github_download_url(
    repository: str, tag: str, filename: str, value: object
) -> None:
    if not isinstance(value, str):
        raise ReleaseVerificationError(
            f"GitHub asset is missing a public download URL: {filename}"
        )
    owner, repo = repository.split("/", 1)
    expected_path = (
        f"/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(repo, safe='')}/"
        f"releases/download/{urllib.parse.quote(tag, safe='')}/"
        f"{urllib.parse.quote(filename, safe='')}"
    )
    parsed = urllib.parse.urlsplit(value)
    if (
        _origin(value) != "https://github.com"
        or parsed.path != expected_path
        or parsed.query
        or parsed.fragment
    ):
        raise ReleaseVerificationError(
            f"GitHub asset has an unexpected public download URL: {filename}"
        )


def _verify_github(
    fetch: Fetcher,
    repository: str,
    tag: str,
    sha: str,
    alias: str,
    version: str,
    release_notes: str,
    local_assets: Mapping[str, LocalArtifact],
    *,
    verify_alias: bool,
) -> None:
    release_url = _github_api_url(
        repository, f"releases/tags/{urllib.parse.quote(tag, safe='')}"
    )
    release = _request_json(
        fetch,
        release_url,
        allowed_origins=frozenset({"https://api.github.com"}),
    )
    if release.get("tag_name") != tag:
        raise ReleaseVerificationError("GitHub Release tag disagrees")
    target = release.get("target_commitish")
    if not isinstance(target, str) or not target:
        raise ReleaseVerificationError(
            "GitHub Release target_commitish must be a non-empty string"
        )
    if release.get("name") != f"{PROJECT} {version}":
        raise ReleaseVerificationError("GitHub Release title disagrees")
    body = release.get("body")
    if not isinstance(body, str) or body.rstrip("\n") != release_notes.rstrip("\n"):
        raise ReleaseVerificationError("GitHub Release notes disagree")
    if release.get("draft") is not False:
        raise ReleaseVerificationError("GitHub Release is still a draft")
    if release.get("prerelease") is not False:
        raise ReleaseVerificationError("GitHub Release is marked as a prerelease")
    if release.get("immutable") is not True:
        raise ReleaseVerificationError("GitHub Release is not immutable")
    if not isinstance(release.get("published_at"), str) or not release["published_at"]:
        raise ReleaseVerificationError("GitHub Release has no publication timestamp")

    assets = release.get("assets")
    if not isinstance(assets, list):
        raise ReleaseVerificationError("GitHub Release assets must be a list")
    remote: dict[str, Mapping[str, Any]] = {}
    for item in assets:
        if not isinstance(item, dict):
            raise ReleaseVerificationError("GitHub Release asset entries must be objects")
        filename = _safe_remote_filename(item.get("name"))
        if filename in remote:
            raise ReleaseVerificationError(f"duplicate GitHub Release asset: {filename}")
        remote[filename] = item
    if set(remote) != set(local_assets):
        raise ReleaseVerificationError(
            "GitHub Release asset names do not exactly match the local release assets"
        )
    for filename, artifact in local_assets.items():
        item = remote[filename]
        size = item.get("size")
        digest = item.get("digest")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size != artifact.size
            or digest != f"sha256:{artifact.sha256}"
            or item.get("state") != "uploaded"
        ):
            raise ReleaseVerificationError(
                f"GitHub Release asset disagrees with local artifact: {filename}"
            )
        _validate_github_download_url(
            repository, tag, filename, item.get("browser_download_url")
        )

    _verify_tag_ref(fetch, repository, tag, sha, "release tag")
    if verify_alias:
        _verify_tag_ref(
            fetch, repository, alias, sha, f"compatibility alias {alias}"
        )


class _EmbeddedDataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._capture = False
        self._parts: list[str] = []
        self.documents: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        values = dict(attrs)
        if tag == "script" and values.get("data-target") == "react-app.embeddedData":
            self._capture = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._capture:
            self.documents.append("".join(self._parts))
            self._capture = False
            self._parts = []


def _verify_marketplace(
    fetch: Fetcher, repository: str, marketplace_slug: str, tag: str
) -> None:
    url = f"https://github.com/marketplace/actions/{marketplace_slug}"
    body = _request(
        fetch,
        url,
        "text/html",
        allowed_origins=frozenset({"https://github.com"}),
    )
    try:
        html = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ReleaseVerificationError("Marketplace returned non-UTF-8 HTML") from error
    parser = _EmbeddedDataParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as error:
        raise ReleaseVerificationError("cannot parse Marketplace HTML") from error

    owner, name = repository.split("/", 1)
    matches = 0
    for document in parser.documents:
        try:
            embedded = json.loads(document)
        except json.JSONDecodeError:
            continue
        if not isinstance(embedded, dict):
            continue
        payload = embedded.get("payload")
        if not isinstance(payload, dict):
            continue
        action = payload.get("action")
        repository_data = payload.get("repository")
        release_data = payload.get("releaseData")
        if not all(
            isinstance(item, dict)
            for item in (action, repository_data, release_data)
        ):
            continue
        latest = release_data.get("latestRelease")
        if not isinstance(latest, dict):
            continue
        if (
            action.get("slug") != marketplace_slug
            or repository_data.get("owner") != owner
            or repository_data.get("name") != name
        ):
            raise ReleaseVerificationError(
                "Marketplace page identity does not match the expected action repository"
            )
        if latest.get("tagName") != tag or latest.get("isPrerelease") is not False:
            raise ReleaseVerificationError(
                f"Marketplace does not report {tag} as the latest stable release"
            )
        matches += 1
    if matches != 1:
        raise ReleaseVerificationError(
            "Marketplace page did not expose exactly one authoritative latest release"
        )


def _prepare_verification(
    *,
    tag: str,
    sha: str,
    alias: str,
    dist_dir: Path,
    release_notes: Path,
    release_assets_dir: Path | None = None,
    repository: str = DEFAULT_REPOSITORY,
    marketplace_slug: str = DEFAULT_MARKETPLACE_SLUG,
    phase: str = "complete",
) -> tuple[str, str, dict[str, LocalArtifact], dict[str, LocalArtifact]]:
    match = TAG_RE.fullmatch(tag)
    if match is None:
        raise ReleaseVerificationError(f"invalid exact release tag: {tag!r}")
    version = tag.removeprefix("v")
    if SHA_RE.fullmatch(sha) is None:
        raise ReleaseVerificationError("release SHA must be 40 lowercase hexadecimal characters")
    if ALIAS_RE.fullmatch(alias) is None:
        raise ReleaseVerificationError(
            "compatibility alias must be an explicit vMAJOR.MINOR tag"
        )
    expected_alias = f"v{match.group(1)}.{match.group(2)}"
    if alias != expected_alias:
        raise ReleaseVerificationError(
            f"compatibility alias {alias!r} does not match release line {expected_alias!r}"
        )
    if REPOSITORY_RE.fullmatch(repository) is None:
        raise ReleaseVerificationError(f"invalid GitHub repository: {repository!r}")
    if SLUG_RE.fullmatch(marketplace_slug) is None:
        raise ReleaseVerificationError(
            f"invalid Marketplace action slug: {marketplace_slug!r}"
        )
    if phase not in PHASES:
        raise ReleaseVerificationError(
            f"phase must be one of: {', '.join(sorted(PHASES))}"
        )

    asset_dir = release_assets_dir if release_assets_dir is not None else dist_dir
    distributions, release_assets = _load_local_artifacts(
        dist_dir, asset_dir, version
    )
    notes = _load_release_notes(release_notes)
    return version, notes, distributions, release_assets


def _verify_prepared_surfaces(
    *,
    fetch: Fetcher,
    version: str,
    tag: str,
    sha: str,
    alias: str,
    repository: str,
    marketplace_slug: str,
    phase: str,
    verify_alias: bool,
    release_notes: str,
    distributions: Mapping[str, LocalArtifact],
    release_assets: Mapping[str, LocalArtifact],
) -> None:
    registries = REGISTRIES if phase == "complete" else (REGISTRIES[1],)
    for registry in registries:
        _verify_registry(fetch, registry, version, distributions)
    _verify_github(
        fetch,
        repository,
        tag,
        sha,
        alias,
        version,
        release_notes,
        release_assets,
        verify_alias=verify_alias,
    )
    _verify_marketplace(fetch, repository, marketplace_slug, tag)


def verify_release_surfaces(
    *,
    tag: str,
    sha: str,
    alias: str,
    dist_dir: Path,
    release_notes: Path,
    release_assets_dir: Path | None = None,
    repository: str = DEFAULT_REPOSITORY,
    marketplace_slug: str = DEFAULT_MARKETPLACE_SLUG,
    phase: str = "complete",
    verify_alias: bool = True,
    fetch: Fetcher | None = None,
) -> None:
    """Raise when any required release surface differs from the candidate."""
    version, notes, distributions, release_assets = _prepare_verification(
        tag=tag,
        sha=sha,
        alias=alias,
        dist_dir=dist_dir,
        release_notes=release_notes,
        release_assets_dir=release_assets_dir,
        repository=repository,
        marketplace_slug=marketplace_slug,
        phase=phase,
    )
    network = fetch if fetch is not None else _stdlib_fetch
    _verify_prepared_surfaces(
        fetch=network,
        version=version,
        tag=tag,
        sha=sha,
        alias=alias,
        repository=repository,
        marketplace_slug=marketplace_slug,
        phase=phase,
        verify_alias=verify_alias and phase == "complete",
        release_notes=notes,
        distributions=distributions,
        release_assets=release_assets,
    )


def verify_release_surfaces_with_retries(
    *,
    tag: str,
    sha: str,
    alias: str,
    dist_dir: Path,
    release_notes: Path,
    release_assets_dir: Path | None = None,
    repository: str = DEFAULT_REPOSITORY,
    marketplace_slug: str = DEFAULT_MARKETPLACE_SLUG,
    phase: str = "complete",
    verify_alias: bool = True,
    attempts: int = 12,
    delay_seconds: float = 10.0,
    fetch: Fetcher | None = None,
    sleep: Sleeper = time.sleep,
    retry_notice: RetryNotice | None = None,
) -> None:
    """Retry bounded public-state checks while failing local validation early."""
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1:
        raise ReleaseVerificationError("attempts must be a positive integer")
    if delay_seconds < 0:
        raise ReleaseVerificationError("delay-seconds must be non-negative")
    version, notes, distributions, release_assets = _prepare_verification(
        tag=tag,
        sha=sha,
        alias=alias,
        dist_dir=dist_dir,
        release_notes=release_notes,
        release_assets_dir=release_assets_dir,
        repository=repository,
        marketplace_slug=marketplace_slug,
        phase=phase,
    )
    network = fetch if fetch is not None else _stdlib_fetch
    for attempt in range(1, attempts + 1):
        try:
            _verify_prepared_surfaces(
                fetch=network,
                version=version,
                tag=tag,
                sha=sha,
                alias=alias,
                repository=repository,
                marketplace_slug=marketplace_slug,
                phase=phase,
                verify_alias=verify_alias and phase == "complete",
                release_notes=notes,
                distributions=distributions,
                release_assets=release_assets,
            )
            return
        except (ReleaseVerificationError, ReleaseNetworkError) as error:
            if attempt == attempts:
                raise
            if retry_notice is not None:
                retry_notice(attempt, attempts, error)
            sleep(delay_seconds)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify exact artifacts across PyPI, TestPyPI, GitHub, and Marketplace."
    )
    parser.add_argument("--tag", required=True, help="Exact vMAJOR.MINOR.PATCH tag")
    parser.add_argument("--sha", required=True, help="Exact 40-character release commit SHA")
    parser.add_argument(
        "--alias", required=True, help="Explicit vMAJOR.MINOR compatibility alias"
    )
    parser.add_argument("--dist-dir", required=True, type=Path)
    parser.add_argument(
        "--release-notes",
        required=True,
        type=Path,
        help="Exact UTF-8 notes file used to publish the GitHub Release",
    )
    parser.add_argument(
        "--release-assets-dir",
        type=Path,
        help="Directory containing the versioned .pyz and SHA256SUMS (default: dist dir)",
    )
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    parser.add_argument("--marketplace-slug", default=DEFAULT_MARKETPLACE_SLUG)
    parser.add_argument(
        "--phase",
        choices=sorted(PHASES),
        default="complete",
        help="marketplace defers production PyPI and the compatibility alias",
    )
    parser.add_argument(
        "--skip-alias",
        action="store_true",
        help="Verify complete registry/release state without resolving a compatibility alias",
    )
    parser.add_argument("--attempts", type=int, default=12)
    parser.add_argument("--delay-seconds", type=float, default=10.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        verify_release_surfaces_with_retries(
            tag=args.tag,
            sha=args.sha,
            alias=args.alias,
            dist_dir=args.dist_dir,
            release_notes=args.release_notes,
            release_assets_dir=args.release_assets_dir,
            repository=args.repository,
            marketplace_slug=args.marketplace_slug,
            phase=args.phase,
            verify_alias=not args.skip_alias,
            attempts=args.attempts,
            delay_seconds=args.delay_seconds,
            retry_notice=lambda attempt, attempts, error: print(
                f"Public state not ready (attempt {attempt}/{attempts}): {error}",
                file=sys.stderr,
            ),
        )
    except (ReleaseVerificationError, ReleaseNetworkError) as error:
        print(f"Release surface verification failed: {error}", file=sys.stderr)
        return 1
    print(
        f"Release surfaces agree for {args.tag} at {args.sha} "
        f"(phase {args.phase}, compatibility alias {args.alias})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
