from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "prepare_oci_scan_layout.py"


def _load_script():
    name = "boundver_prepare_oci_scan_layout_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


oci = _load_script()


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _descriptor(
    payload: bytes, media_type: str, *, platform: dict[str, str] | None = None
) -> dict[str, object]:
    value: dict[str, object] = {
        "mediaType": media_type,
        "digest": _digest(payload),
        "size": len(payload),
    }
    if platform is not None:
        value["platform"] = platform
    return value


def _archive_payloads(
    *, config_architectures: dict[str, str] | None = None
) -> tuple[dict[str, bytes], dict[str, str]]:
    config_architectures = config_architectures or {
        "amd64": "amd64",
        "arm64": "arm64",
    }
    blobs: dict[str, bytes] = {}
    manifest_digests: dict[str, str] = {}
    manifest_descriptors: list[dict[str, object]] = []
    for architecture in ("amd64", "arm64"):
        config = _json_bytes(
            {"architecture": config_architectures[architecture], "os": "linux"}
        )
        blobs[_digest(config).removeprefix("sha256:")] = config
        manifest = _json_bytes(
            {
                "schemaVersion": 2,
                "mediaType": oci.OCI_MANIFEST_MEDIA_TYPE,
                "config": _descriptor(config, oci.OCI_CONFIG_MEDIA_TYPE),
                "layers": [],
            }
        )
        manifest_digest = _digest(manifest)
        manifest_digests[architecture] = manifest_digest
        blobs[manifest_digest.removeprefix("sha256:")] = manifest
        manifest_descriptors.append(
            _descriptor(
                manifest,
                oci.OCI_MANIFEST_MEDIA_TYPE,
                platform={"architecture": architecture, "os": "linux"},
            )
        )
    nested_index = _json_bytes(
        {
            "schemaVersion": 2,
            "mediaType": oci.OCI_INDEX_MEDIA_TYPE,
            "manifests": manifest_descriptors,
        }
    )
    blobs[_digest(nested_index).removeprefix("sha256:")] = nested_index
    root_index = _json_bytes(
        {
            "schemaVersion": 2,
            "mediaType": oci.OCI_INDEX_MEDIA_TYPE,
            "manifests": [_descriptor(nested_index, oci.OCI_INDEX_MEDIA_TYPE)],
        }
    )
    payloads = {
        "oci-layout": _json_bytes({"imageLayoutVersion": "1.0.0"}),
        "index.json": root_index,
        **{f"blobs/sha256/{digest}": payload for digest, payload in blobs.items()},
    }
    return payloads, manifest_digests


def _write_archive(
    path: Path,
    payloads: dict[str, bytes],
    *,
    extra: tuple[tarfile.TarInfo, bytes] | None = None,
) -> None:
    with tarfile.open(path, "w") as archive:
        for directory in ("blobs", "blobs/sha256"):
            member = tarfile.TarInfo(directory)
            member.type = tarfile.DIRTYPE
            member.mode = 0o755
            archive.addfile(member)
        for name, payload in payloads.items():
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(payload))
        if extra is not None:
            member, payload = extra
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))


def test_prepares_digest_verified_selector_for_each_exact_platform(tmp_path: Path):
    archive = tmp_path / "image.oci.tar"
    payloads, manifest_digests = _archive_payloads()
    _write_archive(archive, payloads)
    original_digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    output = tmp_path / "scan"
    oci.prepare_scan_layout(
        archive, output, ["linux/amd64", "linux/arm64"]
    )

    assert hashlib.sha256(archive.read_bytes()).hexdigest() == original_digest
    assert (output / "layout" / "index.json").read_bytes() == payloads["index.json"]
    for architecture in ("amd64", "arm64"):
        selector = json.loads(
            (output / "selectors" / f"linux-{architecture}.json").read_text(
                encoding="utf-8"
            )
        )
        assert selector["schemaVersion"] == 2
        assert selector["mediaType"] == oci.OCI_INDEX_MEDIA_TYPE
        assert len(selector["manifests"]) == 1
        selected = selector["manifests"][0]
        assert selected["digest"] == manifest_digests[architecture]
        assert selected["platform"] == {
            "architecture": architecture,
            "os": "linux",
        }


def test_rejects_archive_path_traversal_without_writing_output(tmp_path: Path):
    archive = tmp_path / "image.oci.tar"
    payloads, _ = _archive_payloads()
    unsafe = tarfile.TarInfo("../escaped")
    _write_archive(archive, payloads, extra=(unsafe, b"unsafe"))

    output = tmp_path / "scan"
    with pytest.raises(oci.OciScanLayoutError, match="unsafe member name"):
        oci.prepare_scan_layout(archive, output, ["linux/amd64", "linux/arm64"])

    assert not output.exists()
    assert not (tmp_path / "escaped").exists()


def test_rejects_non_regular_archive_member(tmp_path: Path):
    archive = tmp_path / "image.oci.tar"
    payloads, _ = _archive_payloads()
    link = tarfile.TarInfo("blobs/sha256/" + "a" * 64)
    link.type = tarfile.SYMTYPE
    link.linkname = "../../index.json"
    _write_archive(archive, payloads, extra=(link, b""))

    with pytest.raises(oci.OciScanLayoutError, match="unsupported member"):
        oci.prepare_scan_layout(
            archive, tmp_path / "scan", ["linux/amd64", "linux/arm64"]
        )


def test_rejects_content_addressed_blob_mismatch(tmp_path: Path):
    archive = tmp_path / "image.oci.tar"
    payloads, _ = _archive_payloads()
    blob_name = next(name for name in payloads if name.startswith("blobs/sha256/"))
    payloads[blob_name] += b"tampered"
    _write_archive(archive, payloads)

    with pytest.raises(oci.OciScanLayoutError, match="blob digest mismatch"):
        oci.prepare_scan_layout(
            archive, tmp_path / "scan", ["linux/amd64", "linux/arm64"]
        )


def test_rejects_descriptor_and_image_config_platform_disagreement(tmp_path: Path):
    archive = tmp_path / "image.oci.tar"
    payloads, _ = _archive_payloads(
        config_architectures={"amd64": "amd64", "arm64": "amd64"}
    )
    _write_archive(archive, payloads)

    with pytest.raises(oci.OciScanLayoutError, match="disagrees with its image config"):
        oci.prepare_scan_layout(
            archive, tmp_path / "scan", ["linux/amd64", "linux/arm64"]
        )


@pytest.mark.parametrize(
    "platforms, message",
    [
        (["linux/amd64"], "platform set disagrees"),
        (["linux/amd64", "linux/amd64"], "must be unique"),
        (["linux"], "expected os/architecture"),
    ],
)
def test_rejects_incomplete_ambiguous_or_malformed_platform_requests(
    tmp_path: Path, platforms: list[str], message: str
):
    archive = tmp_path / "image.oci.tar"
    payloads, _ = _archive_payloads()
    _write_archive(archive, payloads)

    with pytest.raises(oci.OciScanLayoutError, match=message):
        oci.prepare_scan_layout(archive, tmp_path / "scan", platforms)


def test_rejects_existing_output_path(tmp_path: Path):
    archive = tmp_path / "image.oci.tar"
    payloads, _ = _archive_payloads()
    _write_archive(archive, payloads)
    output = tmp_path / "scan"
    output.mkdir()

    with pytest.raises(oci.OciScanLayoutError, match="already exists"):
        oci.prepare_scan_layout(
            archive, output, ["linux/amd64", "linux/arm64"]
        )
