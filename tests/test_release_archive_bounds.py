"""Hard bounds for release-network reads and distribution archive inspection."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import os
import ssl
import sys
import tarfile
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_script(filename: str, module_name: str):
    path = ROOT / "scripts" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - import invariant
        raise AssertionError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


testpypi = _load_script("verify_testpypi_release.py", "bounded_testpypi_verifier")
builder = _load_script("build_release_artifacts.py", "bounded_release_builder")
locker = _load_script("lock_release_tools.py", "bounded_release_tool_locker")


class _Response(io.BytesIO):
    def __init__(
        self,
        payload: bytes,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(payload)
        self._url = url
        self.headers = headers or {}
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return super().read(size)

    def geturl(self) -> str:
        return self._url


def _write_wheel(path: Path, entries: list[tuple[str, bytes]]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in entries:
            archive.writestr(name, payload)


def _write_sdist(path: Path, entries: list[tuple[str, bytes]]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, payload in entries:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))


def test_package_index_openers_disable_ambient_proxies_and_redirects():
    for module, opener_name in (
        (testpypi, "_PUBLIC_OPENER"),
        (locker, "_PYPI_OPENER"),
    ):
        opener = getattr(module, opener_name)
        redirect = next(
            handler
            for handler in opener.handlers
            if isinstance(handler, module._RejectRedirects)
        )
        assert module._NO_PROXY_HANDLER.proxies == {}
        assert (
            redirect.redirect_request(None, None, 302, "Found", {}, "https://evil.invalid")
            is None
        )
        https = next(
            handler
            for handler in opener.handlers
            if isinstance(handler, module.urllib.request.HTTPSHandler)
        )
        assert https._context.minimum_version == ssl.TLSVersion.TLSv1_2


def test_package_index_tls_contexts_ignore_environment_selected_trust():
    hostile = {
        "SSL_CERT_FILE": "repo/attacker-ca.pem",
        "SSL_CERT_DIR": "repo/attacker-certs",
        "SSLKEYLOGFILE": "repo/tls-keys.log",
    }
    for module in (testpypi, locker):
        observed: list[dict[str, str | None]] = []

        def context_factory(*, purpose):
            assert purpose is ssl.Purpose.SERVER_AUTH
            observed.append({name: os.environ.get(name) for name in hostile})
            return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

        with patch.dict(os.environ, hostile, clear=False), patch.object(
            module.ssl,
            "create_default_context",
            side_effect=context_factory,
        ):
            context = module._public_tls_context()
            assert {name: os.environ.get(name) for name in hostile} == hostile

        assert observed == [{name: None for name in hostile}]
        assert context.minimum_version == ssl.TLSVersion.TLSv1_2


def test_testpypi_json_content_length_is_checked_before_read(monkeypatch):
    url = "https://test.pypi.org/pypi/boundver/1.2.3/json"
    response = _Response(b"{}", url, {"Content-Length": "5"})
    monkeypatch.setattr(testpypi, "MAX_INDEX_METADATA_BYTES", 4)
    monkeypatch.setattr(testpypi, "_open_public_url", lambda *args, **kwargs: response)

    with pytest.raises(testpypi.ReleaseNetworkError, match="response limit"):
        testpypi._request_json(url)

    assert response.read_sizes == []


def test_testpypi_json_reader_stops_after_one_growth_byte(monkeypatch):
    url = "https://test.pypi.org/pypi/boundver/1.2.3/json"
    response = _Response(b"abcde-more", url)
    monkeypatch.setattr(testpypi, "MAX_INDEX_METADATA_BYTES", 4)
    monkeypatch.setattr(testpypi, "_open_public_url", lambda *args, **kwargs: response)

    with pytest.raises(testpypi.ReleaseNetworkError, match="4-byte response limit"):
        testpypi._request_json(url)

    assert response.read_sizes == [4, 1]


def test_testpypi_download_stops_at_advertised_size_plus_one(monkeypatch):
    url = "https://test-files.pythonhosted.org/packages/example.whl"
    response = _Response(b"abcde-untrusted-tail", url)
    expected = testpypi.DistributionFile(
        "example.whl",
        hashlib.sha256(b"abcd").hexdigest(),
        4,
        url,
    )
    monkeypatch.setattr(testpypi, "_open_public_url", lambda *args, **kwargs: response)

    with pytest.raises(testpypi.ReleaseVerificationError, match="advertised size"):
        testpypi._download_and_verify({expected.filename: expected}, url.rsplit("/", 2)[0])

    assert response.read_sizes == [4, 1]


def test_wheel_member_count_is_preflighted_before_zipfile_allocation(
    tmp_path, monkeypatch
):
    wheel = tmp_path / "example.whl"
    _write_wheel(wheel, [("one", b""), ("two", b"")])
    monkeypatch.setattr(testpypi, "MAX_ARCHIVE_MEMBERS", 1)
    monkeypatch.setattr(
        testpypi.zipfile,
        "ZipFile",
        lambda *args, **kwargs: pytest.fail("ZipFile must not be constructed"),
    )

    with pytest.raises(testpypi.ReleaseVerificationError, match="1-member"):
        testpypi._metadata_identity(wheel)


def test_wheel_metadata_is_bounded_by_advertised_uncompressed_size(
    tmp_path, monkeypatch
):
    wheel = tmp_path / "example.whl"
    _write_wheel(
        wheel,
        [("example-1.0.dist-info/METADATA", b"Name: example\nVersion: 1.0\n")],
    )
    monkeypatch.setattr(testpypi, "MAX_METADATA_BYTES", 4)

    with pytest.raises(testpypi.ReleaseVerificationError, match="metadata limit"):
        testpypi._metadata_identity(wheel)


def test_wheel_paths_and_aggregate_are_preflighted_before_zipfile(
    tmp_path, monkeypatch
):
    wheel = tmp_path / "example.whl"
    _write_wheel(wheel, [("example/one", b"a"), ("example/two", b"b")])
    monkeypatch.setattr(testpypi, "MAX_ARCHIVE_PATH_BYTES", 32)
    monkeypatch.setattr(testpypi, "MAX_ARCHIVE_TOTAL_BYTES", 1)
    monkeypatch.setattr(
        testpypi.zipfile,
        "ZipFile",
        lambda *args, **kwargs: pytest.fail("ZipFile must not be constructed"),
    )

    with pytest.raises(testpypi.ReleaseVerificationError, match="uncompressed-byte"):
        testpypi._metadata_identity(wheel)


def test_sdist_member_count_is_preflighted_before_tarfile_objects(
    tmp_path, monkeypatch
):
    sdist = tmp_path / "example.tar.gz"
    _write_sdist(sdist, [("example/one", b""), ("example/two", b"")])
    real_open = testpypi.tarfile.open
    monkeypatch.setattr(testpypi, "MAX_ARCHIVE_MEMBERS", 1)
    monkeypatch.setattr(
        testpypi.tarfile,
        "open",
        lambda *args, **kwargs: pytest.fail("tarfile.open must not be called"),
    )

    try:
        with pytest.raises(testpypi.ReleaseVerificationError, match="1-member"):
            testpypi._metadata_identity(sdist)
    finally:
        monkeypatch.setattr(testpypi.tarfile, "open", real_open)


def test_normal_bounded_candidate_metadata_remains_accepted(tmp_path):
    wheel = tmp_path / "example-1.0-py3-none-any.whl"
    sdist = tmp_path / "example-1.0.tar.gz"
    metadata = b"Name: example\nVersion: 1.0\n"
    _write_wheel(wheel, [("example-1.0.dist-info/METADATA", metadata)])
    _write_sdist(sdist, [("example-1.0/PKG-INFO", metadata)])

    candidate = testpypi._load_candidate(tmp_path, "example", "1.0")

    assert set(candidate) == {wheel.name, sdist.name}


def test_wheel_rejects_duplicate_non_metadata_members(tmp_path):
    wheel = tmp_path / "example.whl"
    metadata = b"Name: example\nVersion: 1.0\n"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        _write_wheel(
            wheel,
            [
                ("example/data.txt", b"first"),
                ("example/data.txt", b"second"),
                ("example-1.0.dist-info/METADATA", metadata),
            ],
        )

    with pytest.raises(testpypi.ReleaseVerificationError, match="duplicate"):
        testpypi._metadata_identity(wheel)


def test_sdist_rejects_duplicate_non_metadata_members(tmp_path):
    sdist = tmp_path / "example.tar.gz"
    metadata = b"Name: example\nVersion: 1.0\n"
    _write_sdist(
        sdist,
        [
            ("example/data.txt", b"first"),
            ("example/data.txt", b"second"),
            ("example/PKG-INFO", metadata),
        ],
    )

    with pytest.raises(testpypi.ReleaseVerificationError, match="duplicate"):
        testpypi._metadata_identity(sdist)


@pytest.mark.parametrize(
    "name",
    (
        "example/data.txt:stream",
        "example/CON.txt",
        "example/trailing.",
        "example/control\x1f.txt",
    ),
)
def test_release_validators_reject_nonportable_archive_names(name):
    with pytest.raises(ValueError, match="unsafe or overlong"):
        builder._validate_archive_path(name, "example.whl")
    with pytest.raises(testpypi.ReleaseVerificationError, match="unsafe or overlong"):
        testpypi._validate_archive_path(name, "example.whl")


@pytest.mark.parametrize(
    "first,second",
    (
        ("example/Contract.json", "example/contract.json"),
        ("example/caf\N{LATIN SMALL LETTER E WITH ACUTE}.json", "example/cafe\u0301.json"),
    ),
)
def test_wheel_rejects_portable_name_collisions(tmp_path, first, second):
    wheel = tmp_path / "example.whl"
    metadata = b"Name: example\nVersion: 1.0\n"
    _write_wheel(
        wheel,
        [
            (first, b"first"),
            (second, b"second"),
            ("example-1.0.dist-info/METADATA", metadata),
        ],
    )

    with pytest.raises(testpypi.ReleaseVerificationError, match="non-portable"):
        testpypi._metadata_identity(wheel)
    with pytest.raises(ValueError, match="non-portable"):
        builder._canonicalize_wheel(
            wheel,
            tmp_path / "canonical.whl",
            1_700_000_000,
        )


def test_sdist_rejects_portable_name_collisions(tmp_path):
    sdist = tmp_path / "example.tar.gz"
    metadata = b"Name: example\nVersion: 1.0\n"
    _write_sdist(
        sdist,
        [
            ("example/Contract.json", b"first"),
            ("example/contract.json", b"second"),
            ("example/PKG-INFO", metadata),
        ],
    )

    with pytest.raises(testpypi.ReleaseVerificationError, match="non-portable"):
        testpypi._metadata_identity(sdist)
    with pytest.raises(ValueError, match="non-portable"):
        builder._canonicalize_sdist(
            sdist,
            tmp_path / "canonical.tar.gz",
            1_700_000_000,
        )


def test_builder_rejects_source_zip_size_before_zipfile(tmp_path, monkeypatch):
    wheel = tmp_path / "example.whl"
    _write_wheel(wheel, [("example", b"payload")])
    monkeypatch.setattr(builder, "MAX_SOURCE_ARCHIVE_BYTES", wheel.stat().st_size - 1)
    monkeypatch.setattr(
        builder,
        "ZipFile",
        lambda *args, **kwargs: pytest.fail("ZipFile must not be constructed"),
    )

    with pytest.raises(ValueError, match="source archive exceeds"):
        builder._canonicalize_wheel(wheel, tmp_path / "canonical.whl", 1_700_000_000)


def test_builder_preflights_zip_member_size_before_zipfile(tmp_path, monkeypatch):
    wheel = tmp_path / "example.whl"
    _write_wheel(wheel, [("example", b"ab")])
    monkeypatch.setattr(builder, "MAX_ARCHIVE_MEMBER_BYTES", 1)
    monkeypatch.setattr(
        builder,
        "ZipFile",
        lambda *args, **kwargs: pytest.fail("ZipFile must not be constructed"),
    )

    with pytest.raises(ValueError, match="oversized archive member"):
        builder._canonicalize_wheel(wheel, tmp_path / "canonical.whl", 1_700_000_000)


def test_builder_sdist_canonicalization_streams_final_gzip(tmp_path, monkeypatch):
    sdist = tmp_path / "example.tar.gz"
    canonical = tmp_path / "canonical.tar.gz"
    _write_sdist(
        sdist,
        [("example/PKG-INFO", b"Name: example\nVersion: 1.0\n")],
    )
    monkeypatch.setattr(
        builder,
        "_stored_gzip",
        lambda *args, **kwargs: pytest.fail("whole-archive encoder must not be used"),
    )

    builder._canonicalize_sdist(sdist, canonical, 1_700_000_000)

    with tarfile.open(canonical, "r:gz") as archive:
        assert archive.getnames() == ["example/PKG-INFO"]


def test_streamed_gzip_preserves_the_canonical_wire_bytes(tmp_path):
    payload = (b"canonical-tar-block" * 5000) + b"tail"
    source = tmp_path / "canonical.tar"
    destination = tmp_path / "canonical.tar.gz"
    source.write_bytes(payload)

    builder._write_stored_gzip(source, destination, 1_700_000_000)

    assert destination.read_bytes() == builder._stored_gzip(payload, 1_700_000_000)


def test_locker_pypi_content_length_is_checked_before_read(monkeypatch):
    requirement = locker.Requirement("example", "1.0")
    response = _Response(b"{}", "https://pypi.org", {"Content-Length": "5"})
    monkeypatch.setattr(locker, "MAX_PYPI_METADATA_BYTES", 4)
    monkeypatch.setattr(locker, "_open_pypi_url", lambda *args, **kwargs: response)

    with pytest.raises(locker.LockError, match="response limit"):
        locker._pypi_release(requirement)

    assert response.read_sizes == []


def test_locker_rejects_pypi_metadata_redirected_off_origin(monkeypatch):
    requirement = locker.Requirement("example", "1.0")
    response = _Response(b"{}", "https://attacker.invalid/example.json")
    monkeypatch.setattr(locker, "_open_pypi_url", lambda *args, **kwargs: response)

    with pytest.raises(locker.LockError, match="canonical PyPI origin"):
        locker._pypi_release(requirement)

    assert response.read_sizes == []


def test_locker_manifest_size_is_rejected_before_open(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.toml"
    manifest.write_bytes(b"ab")
    monkeypatch.setattr(locker, "MAX_MANIFEST_BYTES", 1)

    def fail_open(*args, **kwargs):
        raise AssertionError("oversized manifest must not be opened")

    monkeypatch.setattr(Path, "open", fail_open)
    with pytest.raises(locker.LockError, match="1-byte limit"):
        locker.load_manifest(manifest)


def test_locker_artifact_evidence_size_is_rejected_before_open(
    tmp_path, monkeypatch
):
    evidence = tmp_path / "artifacts.json"
    evidence.write_bytes(b"ab")
    monkeypatch.setattr(locker, "MAX_ARTIFACT_EVIDENCE_BYTES", 1)

    def fail_open(*args, **kwargs):
        raise AssertionError("oversized evidence must not be opened")

    monkeypatch.setattr(Path, "open", fail_open)
    with pytest.raises(locker.LockError, match="1-byte limit"):
        locker._load_artifacts(evidence)


def test_locker_streamed_comparison_preflights_lock_size(tmp_path, monkeypatch):
    lock = tmp_path / "requirements.lock"
    lock.write_bytes(b"ab")

    def fail_open(*args, **kwargs):
        raise AssertionError("oversized lock must not be opened")

    monkeypatch.setattr(Path, "open", fail_open)
    with pytest.raises(locker.LockError, match="1-byte limit"):
        locker._file_matches(lock, b"a", 1, "requirements lock")
