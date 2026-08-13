#!/usr/bin/env python3
"""Fail-closed package-index artifact comparison for the release workflow.

The script intentionally uses only the Python standard library.  It compares
the local wheel and sdist with one package-index release by filename, byte length,
and SHA-256, then downloads every advertised file and hashes the bytes again.
"""

from __future__ import annotations

import argparse
import email.parser
import hashlib
import json
import re
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


DEFAULT_API_BASE = "https://test.pypi.org/pypi"
DEFAULT_DOWNLOAD_ORIGIN = "https://test-files.pythonhosted.org"
USER_AGENT = "boundver-release-verifier/1"
SHA256_RE = re.compile(r"[0-9a-f]{64}")
PROJECT_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


class ReleaseVerificationError(ValueError):
    """A permanent integrity or release-shape failure."""


class ReleaseIncompleteError(RuntimeError):
    """A release is missing or has not exposed every candidate file yet."""


class ReleaseNetworkError(RuntimeError):
    """A remote request failed and may succeed on a later attempt."""


@dataclass(frozen=True)
class DistributionFile:
    filename: str
    sha256: str
    size: int
    url: str | None = None


def _normalized_project(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _metadata_identity(path: Path) -> tuple[str, str]:
    try:
        if path.name.endswith(".whl"):
            with zipfile.ZipFile(path) as archive:
                matches = [
                    name
                    for name in archive.namelist()
                    if name.endswith(".dist-info/METADATA")
                ]
                if len(matches) != 1:
                    raise ReleaseVerificationError(
                        f"{path.name} must contain exactly one wheel METADATA file"
                    )
                payload = archive.read(matches[0])
        elif path.name.endswith(".tar.gz"):
            with tarfile.open(path, mode="r:gz") as archive:
                matches = [
                    member
                    for member in archive.getmembers()
                    if member.isfile()
                    and member.name.count("/") == 1
                    and member.name.endswith("/PKG-INFO")
                ]
                if len(matches) != 1:
                    raise ReleaseVerificationError(
                        f"{path.name} must contain exactly one top-level PKG-INFO file"
                    )
                stream = archive.extractfile(matches[0])
                if stream is None:  # pragma: no cover - guarded by member.isfile()
                    raise ReleaseVerificationError(
                        f"cannot read {matches[0].name} from {path.name}"
                    )
                payload = stream.read()
        else:  # pragma: no cover - guarded by _load_candidate()
            raise ReleaseVerificationError(f"unsupported distribution: {path.name}")
    except (OSError, tarfile.TarError, zipfile.BadZipFile, KeyError) as error:
        raise ReleaseVerificationError(
            f"cannot inspect distribution metadata in {path.name}: {error}"
        ) from error

    metadata = email.parser.BytesParser().parsebytes(payload)
    name = metadata.get("Name")
    version = metadata.get("Version")
    if not name or not version:
        raise ReleaseVerificationError(
            f"{path.name} metadata must contain Name and Version"
        )
    return name, version


def _load_candidate(
    dist_dir: Path, project: str, version: str
) -> dict[str, DistributionFile]:
    if not dist_dir.is_dir():
        raise ReleaseVerificationError(f"distribution directory is missing: {dist_dir}")

    entries = sorted(dist_dir.iterdir(), key=lambda item: item.name)
    if any(not item.is_file() or item.is_symlink() for item in entries):
        raise ReleaseVerificationError(
            f"{dist_dir} must contain only regular distribution files"
        )
    wheels = [item for item in entries if item.name.endswith(".whl")]
    sdists = [item for item in entries if item.name.endswith(".tar.gz")]
    if len(entries) != 2 or len(wheels) != 1 or len(sdists) != 1:
        raise ReleaseVerificationError(
            f"{dist_dir} must contain exactly one wheel and one .tar.gz sdist"
        )

    expected_project = _normalized_project(project)
    candidate: dict[str, DistributionFile] = {}
    for path in entries:
        metadata_project, metadata_version = _metadata_identity(path)
        if _normalized_project(metadata_project) != expected_project:
            raise ReleaseVerificationError(
                f"{path.name} project {metadata_project!r} does not match {project!r}"
            )
        if metadata_version != version:
            raise ReleaseVerificationError(
                f"{path.name} version {metadata_version!r} does not match {version!r}"
            )
        candidate[path.name] = DistributionFile(
            filename=path.name,
            sha256=_sha256_file(path),
            size=path.stat().st_size,
        )
    return candidate


def _request_json(url: str) -> Mapping[str, Any] | None:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            requested = urllib.parse.urlsplit(url)
            final = urllib.parse.urlsplit(response.geturl())
            if final.scheme != requested.scheme or final.netloc != requested.netloc:
                raise ReleaseVerificationError(
                    f"TestPyPI metadata redirected outside {requested.scheme}://"
                    f"{requested.netloc}: {response.geturl()!r}"
                )
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise ReleaseNetworkError(f"package index returned HTTP {error.code}: {url}") from error
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
        raise ReleaseNetworkError(f"cannot read package-index release metadata: {error}") from error
    if not isinstance(payload, dict):
        raise ReleaseVerificationError("package-index release response must be a JSON object")
    return payload


def _release_url(api_base: str, project: str, version: str) -> str:
    return (
        f"{api_base.rstrip('/')}/"
        f"{urllib.parse.quote(project, safe='')}/"
        f"{urllib.parse.quote(version, safe='')}/json"
    )


def _validate_download_url(url: str, download_origin: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    expected = urllib.parse.urlsplit(download_origin)
    if (
        parsed.scheme != expected.scheme
        or parsed.netloc != expected.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "\n" in url
        or "\r" in url
    ):
        raise ReleaseVerificationError(
            f"package index supplied a distribution URL outside {download_origin}: {url!r}"
        )


def _parse_remote_release(
    payload: Mapping[str, Any],
    project: str,
    version: str,
    download_origin: str,
) -> dict[str, DistributionFile]:
    info = payload.get("info")
    urls = payload.get("urls")
    if not isinstance(info, dict) or not isinstance(urls, list):
        raise ReleaseVerificationError("package-index response is missing info or urls")
    remote_project = info.get("name")
    remote_version = info.get("version")
    if not isinstance(remote_project, str) or (
        _normalized_project(remote_project) != _normalized_project(project)
    ):
        raise ReleaseVerificationError(
            f"package-index project identity {remote_project!r} does not match {project!r}"
        )
    if remote_version != version:
        raise ReleaseVerificationError(
            f"package-index version identity {remote_version!r} does not match {version!r}"
        )

    remote: dict[str, DistributionFile] = {}
    for item in urls:
        if not isinstance(item, dict):
            raise ReleaseVerificationError("package-index urls entries must be objects")
        filename = item.get("filename")
        digest_map = item.get("digests")
        size = item.get("size")
        url = item.get("url")
        if (
            not isinstance(filename, str)
            or not filename
            or Path(filename).name != filename
            or "/" in filename
            or "\\" in filename
            or not isinstance(digest_map, dict)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(url, str)
        ):
            raise ReleaseVerificationError("package index supplied malformed file metadata")
        sha256 = digest_map.get("sha256")
        if not isinstance(sha256, str) or SHA256_RE.fullmatch(sha256) is None:
            raise ReleaseVerificationError(
                f"package index supplied an invalid SHA-256 for {filename!r}"
            )
        if item.get("yanked") is not False:
            raise ReleaseVerificationError(f"package-index file is yanked: {filename}")
        _validate_download_url(url, download_origin)
        if urllib.parse.unquote(Path(urllib.parse.urlsplit(url).path).name) != filename:
            raise ReleaseVerificationError(
                f"package-index filename and URL disagree for {filename!r}"
            )
        if filename in remote:
            raise ReleaseVerificationError(
                f"package index supplied duplicate file metadata for {filename!r}"
            )
        remote[filename] = DistributionFile(filename, sha256, size, url)
    return remote


def _compare_release(
    candidate: Mapping[str, DistributionFile],
    remote: Mapping[str, DistributionFile],
) -> bool:
    unexpected = sorted(set(remote) - set(candidate))
    if unexpected:
        raise ReleaseVerificationError(
            f"package-index release has unexpected files: {', '.join(unexpected)}"
        )
    for filename, remote_file in remote.items():
        local_file = candidate[filename]
        if (
            remote_file.sha256 != local_file.sha256
            or remote_file.size != local_file.size
        ):
            raise ReleaseVerificationError(
                f"package-index file does not match the candidate: {filename}"
            )
    return set(remote) == set(candidate)


def _download_and_verify(
    files: Mapping[str, DistributionFile], download_origin: str
) -> None:
    for filename in sorted(files):
        expected = files[filename]
        if expected.url is None:  # pragma: no cover - only remote files are passed
            raise ReleaseVerificationError(f"missing download URL for {filename}")
        _validate_download_url(expected.url, download_origin)
        request = urllib.request.Request(
            expected.url,
            headers={
                "Accept": "application/octet-stream",
                "User-Agent": USER_AGENT,
            },
        )
        digest = hashlib.sha256()
        size = 0
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                _validate_download_url(response.geturl(), download_origin)
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    digest.update(chunk)
                    size += len(chunk)
        except (OSError, urllib.error.URLError) as error:
            raise ReleaseNetworkError(
                f"cannot download package-index file {filename}: {error}"
            ) from error
        if digest.hexdigest() != expected.sha256 or size != expected.size:
            raise ReleaseVerificationError(
                f"downloaded package-index bytes do not match advertised file: {filename}"
            )


def _query_release(
    *,
    api_base: str,
    download_origin: str,
    project: str,
    version: str,
) -> dict[str, DistributionFile] | None:
    payload = _request_json(_release_url(api_base, project, version))
    if payload is None:
        return None
    return _parse_remote_release(payload, project, version, download_origin)


def _write_output(path: Path | None, key: str, value: str) -> None:
    if "\n" in value or "\r" in value:
        raise ReleaseVerificationError(f"unsafe workflow output value for {key}")
    if path is None:
        print(f"{key}={value}")
        return
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(f"{key}={value}\n")


def _preflight(args: argparse.Namespace) -> None:
    candidate = _load_candidate(args.dist, args.project, args.version)
    remote = _query_release(
        api_base=args.api_base,
        download_origin=args.download_origin,
        project=args.project,
        version=args.version,
    )
    if remote is None:
        print(f"Package-index release {args.project} {args.version} does not exist yet")
        _write_output(args.github_output, "upload-required", "true")
        _write_output(
            args.github_output, "missing-files", ",".join(sorted(candidate))
        )
        return

    complete = _compare_release(candidate, remote)
    _download_and_verify(remote, args.download_origin)
    if complete:
        print("Package index already contains the exact candidate; upload is unnecessary")
        _write_output(args.github_output, "upload-required", "false")
        _write_output(args.github_output, "missing-files", "")
    else:
        missing = sorted(set(candidate) - set(remote))
        print(
            "Package index contains an exact partial candidate; missing files will be "
            f"uploaded: {', '.join(missing)}"
        )
        _write_output(args.github_output, "upload-required", "true")
        _write_output(args.github_output, "missing-files", ",".join(missing))


def _verify(args: argparse.Namespace) -> None:
    candidate = _load_candidate(args.dist, args.project, args.version)
    last_incomplete = "release is not visible"
    for attempt in range(1, args.attempts + 1):
        try:
            remote = _query_release(
                api_base=args.api_base,
                download_origin=args.download_origin,
                project=args.project,
                version=args.version,
            )
            if remote is None:
                raise ReleaseIncompleteError("release is not visible")
            if not _compare_release(candidate, remote):
                missing = sorted(set(candidate) - set(remote))
                raise ReleaseIncompleteError(
                    f"release is missing: {', '.join(missing)}"
                )
            _download_and_verify(remote, args.download_origin)
        except (ReleaseIncompleteError, ReleaseNetworkError) as error:
            last_incomplete = str(error)
            if attempt == args.attempts:
                break
            print(
                f"Package index is not ready ({attempt}/{args.attempts}): {error}",
                file=sys.stderr,
            )
            time.sleep(args.delay_seconds)
            continue

        wheel = next(item for item in remote.values() if item.filename.endswith(".whl"))
        sdist = next(
            item for item in remote.values() if item.filename.endswith(".tar.gz")
        )
        assert wheel.url is not None  # narrowed by a complete remote release
        assert sdist.url is not None
        _write_output(
            args.github_output, "wheel-url", f"{wheel.url}#sha256={wheel.sha256}"
        )
        _write_output(
            args.github_output, "sdist-url", f"{sdist.url}#sha256={sdist.sha256}"
        )
        print(
            f"Package index contains the exact {args.project} {args.version} candidate"
        )
        return
    raise ReleaseIncompleteError(
        f"Package index did not expose the complete candidate after "
        f"{args.attempts} attempt(s): {last_incomplete}"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare exact local distributions with one TestPyPI release."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("preflight", "verify"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--dist", type=Path, required=True)
        subparser.add_argument("--project", required=True)
        subparser.add_argument("--version", required=True)
        subparser.add_argument("--api-base", default=DEFAULT_API_BASE)
        subparser.add_argument(
            "--download-origin", default=DEFAULT_DOWNLOAD_ORIGIN
        )
        subparser.add_argument("--github-output", type=Path)
        if command == "verify":
            subparser.add_argument("--attempts", type=int, default=12)
            subparser.add_argument("--delay-seconds", type=float, default=10.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if PROJECT_RE.fullmatch(args.project) is None:
        parser.error("--project must be a valid Python distribution name")
    if VERSION_RE.fullmatch(args.version) is None:
        parser.error("--version must be an exact X.Y.Z release")
    if args.command == "verify" and (
        args.attempts < 1 or args.delay_seconds < 0
    ):
        parser.error("--attempts must be positive and --delay-seconds non-negative")

    try:
        if args.command == "preflight":
            _preflight(args)
        else:
            _verify(args)
    except (ReleaseVerificationError, ReleaseIncompleteError, ReleaseNetworkError) as error:
        print(f"Package-index verification failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
