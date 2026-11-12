"""The temporary upstream-fixed assessment cannot become a blanket Git ignore."""

from __future__ import annotations

import copy
import base64
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tarfile
import zlib

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "git_vex_test", ROOT / "scripts/prepare_git_vex.py"
)
vex = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(vex)
SHA = "1" * 40


def encoded(value):
    return json.dumps(value, sort_keys=True).encode()


def layer_bytes(payloads):
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w") as archive:
        for path, payload in payloads.items():
            info = tarfile.TarInfo(path)
            if isinstance(payload, tuple):
                info.type, info.linkname = payload
                archive.addfile(info)
            else:
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
    return result.getvalue()


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    source_root = tmp_path / "source"
    source_root.mkdir()
    components = {
        name: (3, hashlib.sha256(b"src").hexdigest())
        for name in ("git-2.50.2.tar.xz", "git-2.50.2.tar.sign", "git-release-key.gpg")
    }
    trusted = SimpleNamespace(
        SOURCE_FILES=components,
        ELFS={"usr/bin/git"},
        source_metadata=lambda version: {"source_version": version},
    )
    monkeypatch.setattr(vex, "source_audit", lambda: trusted)
    monkeypatch.setattr(
        vex.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=SHA + "\n")
    )
    payloads = {vex.SOURCE + name: b"src" for name in components}
    for entry in vex.RECIPE:
        path = source_root / entry
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"reviewed source")
        payloads[vex.SOURCE + "recipe/" + entry] = b"reviewed source"
    payloads["usr/bin/git"] = b"built Git executable"
    build = {
        "architecture": "amd64",
        "source_version": "1:2.50.2",
        "binary_version": "1:2.50.2+boundver1",
        "upstream_authentication": trusted.source_metadata("1:2.50.2"),
        "source_components": {key: value[1] for key, value in components.items()},
        "elfs": [
            {"path": "usr/bin/git", "sha256": vex.digest(payloads["usr/bin/git"])}
        ],
    }
    payloads[vex.SOURCE + "build.json"] = encoded(build)
    payloads[vex.SOURCE + "upstream.json"] = encoded(
        trusted.source_metadata("1:2.50.2")
    )
    payloads["var/lib/dpkg/status"] = (
        b"Package: git\nStatus: install ok installed\nVersion: 1:2.50.2+boundver1\n"
        b"Source: git (1:2.50.2)\nArchitecture: amd64\n\n"
    )
    return tmp_path, source_root, payloads


def archive_and_report(evidence, arch="amd64"):
    tmp_path, source_root, payloads = evidence
    payloads = copy.deepcopy(payloads)
    build = json.loads(payloads[vex.SOURCE + "build.json"])
    build["architecture"] = arch
    payloads[vex.SOURCE + "build.json"] = encoded(build)
    payloads["var/lib/dpkg/status"] = payloads["var/lib/dpkg/status"].replace(
        b"amd64", arch.encode()
    )
    layer = layer_bytes(payloads)
    config = {
        "os": "linux",
        "architecture": arch,
        "config": {"Labels": {"org.opencontainers.image.revision": SHA}},
        "rootfs": {"type": "layers", "diff_ids": ["sha256:" + vex.digest(layer)]},
    }
    config_bytes = encoded(config)
    image = tmp_path / "image.tar"
    image.write_bytes(
        layer_bytes(
            {
                vex.digest(config_bytes) + ".json": config_bytes,
                "layer.tar": layer,
                "manifest.json": encoded(
                    [
                        {
                            "Config": vex.digest(config_bytes) + ".json",
                            "Layers": ["layer.tar"],
                        }
                    ]
                ),
            }
        )
    )
    purl = (
        f"pkg:deb/debian/git@2.50.2%2Bboundver1?arch={arch}&distro=debian-13.7&epoch=1"
    )
    findings = [
        {
            "VulnerabilityID": cve,
            "PkgName": "git",
            "InstalledVersion": "1:2.50.2+boundver1",
            "PkgIdentifier": {"PURL": purl},
            "DataSource": {"ID": "debian"},
            "Severity": "HIGH",
        }
        for cve in vex.CVES
    ]
    raw = {
        "SchemaVersion": 2,
        "Trivy": {"Version": "0.73.0"},
        "ArtifactType": "container_image",
        "Metadata": {
            "ImageID": "sha256:" + vex.digest(config_bytes),
            "ImageConfig": config,
            "DiffIDs": config["rootfs"]["diff_ids"],
            "OS": {"Family": "debian", "Name": "13.7"},
        },
        "Results": [
            {
                "Class": "os-pkgs",
                "Type": "debian",
                "Target": "debian",
                "Vulnerabilities": findings,
            }
        ],
    }
    report = tmp_path / "raw.json"
    report.write_bytes(encoded(raw))
    return image, report, raw, source_root


@pytest.mark.parametrize("arch", ["amd64", "arm64"])
def test_exact_source_and_image_create_only_three_fixed_statements(evidence, arch):
    image, report, raw, source_root = archive_and_report(evidence, arch)
    output = evidence[0] / "vex.json"
    vex.prepare(image, report, source_root, SHA, arch, output)
    doc = json.loads(output.read_bytes())
    assert (
        doc["boundver_image_binding"]["image_config_digest"]
        == raw["Metadata"]["ImageID"]
    )
    assert len(doc["statements"]) == 3
    assert {s["vulnerability"]["name"] for s in doc["statements"]} == set(vex.CVES)
    assert all(
        s["status"] == "fixed" and len(s["products"]) == 1 for s in doc["statements"]
    )
    scoped = copy.deepcopy(raw)
    result = scoped["Results"][0]
    result["ExperimentalModifiedFindings"] = [
        {"Type": "vulnerability", "Status": "fixed", "Finding": f}
        for f in result.pop("Vulnerabilities")
    ]
    scoped_path = evidence[0] / "scoped.json"
    scoped_path.write_bytes(encoded(scoped))
    vex.verify_scoped(image, report, scoped_path, output)


@pytest.mark.parametrize(
    "change",
    [
        "image_id",
        "image_config",
        "diff_id",
        "revision",
        "scanner",
        "suppressed",
        "purl",
        "fixable",
    ],
)
def test_wrong_scan_or_unreviewed_finding_emits_nothing(evidence, change):
    image, report, raw, source_root = archive_and_report(evidence)
    if change == "image_id":
        raw["Metadata"]["ImageID"] = "sha256:" + "0" * 64
    elif change == "image_config":
        raw["Metadata"]["ImageConfig"]["architecture"] = "arm64"
    elif change == "diff_id":
        raw["Metadata"]["DiffIDs"] = ["sha256:" + "0" * 64]
    elif change == "revision":
        raw["Metadata"]["ImageConfig"]["config"]["Labels"][
            "org.opencontainers.image.revision"
        ] = "0" * 40
    elif change == "scanner":
        raw["Trivy"]["Version"] = "other"
    elif change == "suppressed":
        raw["Results"][0]["ExperimentalModifiedFindings"] = [{}]
    elif change == "purl":
        raw["Results"][0]["Vulnerabilities"][0]["PkgIdentifier"]["PURL"] = (
            "pkg:deb/debian/git"
        )
    else:
        raw["Results"][0]["Vulnerabilities"][0]["FixedVersion"] = "future-fixed-version"
    report.write_bytes(encoded(raw))
    output = evidence[0] / "vex.json"
    with pytest.raises(vex.VexError):
        vex.prepare(image, report, source_root, SHA, "amd64", output)
    assert not output.exists()


@pytest.mark.parametrize(
    "change",
    [
        "elf",
        "recipe",
        "source",
        "old_git",
        "source_manifest",
        "package_source",
        "architecture",
    ],
)
def test_changed_runtime_or_untrusted_provenance_emits_nothing(evidence, change):
    payloads = evidence[2]
    if change == "elf":
        payloads["usr/bin/git"] = b"tampered"
    elif change == "recipe":
        payloads[vex.SOURCE + "recipe/Dockerfile"] = b"different recipe"
    elif change == "source":
        payloads[vex.SOURCE + "git-2.50.2.tar.xz"] = b"bad"
    elif change == "source_manifest":
        payloads[vex.SOURCE + "upstream.json"] = b"{}"
    elif change == "package_source":
        payloads["var/lib/dpkg/status"] = payloads["var/lib/dpkg/status"].replace(
            b"Source: git (1:2.50.2)", b"Source: renamed-to-evade-scanner"
        )
    else:
        build = json.loads(payloads[vex.SOURCE + "build.json"])
        build["source_version" if change == "old_git" else "binary_version"] = (
            "1:2.47.3"
        )
        payloads[vex.SOURCE + "build.json"] = encoded(build)
    image, report, _, source_root = archive_and_report(evidence)
    output = evidence[0] / "vex.json"
    with pytest.raises(vex.VexError):
        vex.prepare(
            image,
            report,
            source_root,
            SHA,
            "arm64" if change == "architecture" else "amd64",
            output,
        )
    assert not output.exists()


@pytest.mark.parametrize(
    "change",
    [
        "new_cve",
        "other_package",
        "not_affected",
        "missing",
        "duplicate",
        "residual",
        "changed_input",
        "other_image",
    ],
)
def test_scoped_report_cannot_lose_or_broaden_findings(evidence, change):
    image, report, raw, source_root = archive_and_report(evidence)
    output = evidence[0] / "vex.json"
    vex.prepare(image, report, source_root, SHA, "amd64", output)
    scoped = copy.deepcopy(raw)
    result = scoped["Results"][0]
    modified = [
        {"Type": "vulnerability", "Status": "fixed", "Finding": f}
        for f in result.pop("Vulnerabilities")
    ]
    result["ExperimentalModifiedFindings"] = modified
    if change == "new_cve":
        modified[0]["Finding"]["VulnerabilityID"] = "CVE-2026-999999"
    elif change == "other_package":
        modified[0]["Finding"]["PkgName"] = "libc6"
    elif change == "not_affected":
        modified[0]["Status"] = "not_affected"
    elif change == "missing":
        modified.pop()
    elif change == "duplicate":
        modified.append(modified[0])
    elif change == "residual":
        result["Vulnerabilities"] = [modified[0]["Finding"]]
    elif change == "other_image":
        scoped["Metadata"]["ImageID"] = "sha256:" + "0" * 64
    else:
        with image.open("ab") as stream:
            stream.write(b"changed image input")
    scoped_path = evidence[0] / "scoped.json"
    scoped_path.write_bytes(encoded(scoped))
    with pytest.raises(vex.VexError):
        vex.verify_scoped(image, report, scoped_path, output)


def test_layer_digest_must_match_even_with_intact_selected_files():
    layer = layer_bytes({"usr/bin/git": b"git"})
    with pytest.raises(vex.VexError, match="diffID"):
        vex.filesystem([lambda: io.BytesIO(layer)], ["sha256:" + "0" * 64])


@pytest.mark.parametrize("path", ["../escape", "/absolute", "a/../b", "a\\b", "a\x1b"])
def test_layer_paths_fail_closed(path):
    with pytest.raises(vex.VexError):
        vex.name(path)


def test_whiteout_removes_previously_seen_evidence():
    first = layer_bytes({"usr/bin/git": b"git"})
    second = layer_bytes({"usr/bin/.wh.git": b""})
    files = vex.filesystem(
        [lambda: io.BytesIO(first), lambda: io.BytesIO(second)],
        ["sha256:" + vex.digest(first), "sha256:" + vex.digest(second)],
    )
    with pytest.raises(vex.VexError):
        vex.file_record(files, "usr/bin/git")


def test_unknown_hardlink_fails_closed():
    layer = layer_bytes({"usr/bin/git": (tarfile.LNKTYPE, "missing")})
    with pytest.raises(vex.VexError):
        vex.filesystem([lambda: io.BytesIO(layer)], ["sha256:" + vex.digest(layer)])


@pytest.mark.parametrize(
    "raw",
    [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":1e999}', b'{"a":' + b"1" * 100 + b"}"],
)
def test_duplicate_nonfinite_or_unbounded_json_is_rejected(raw):
    with pytest.raises(vex.VexError):
        vex.json_value(raw)


@pytest.mark.parametrize("arch", ["amd64", "arm64"])
def test_exact_prepared_oci_selector_with_gzip_layer(evidence, arch):
    image, report, raw, source_root = archive_and_report(evidence, arch)
    layout = evidence[0] / "layout"
    blobs = layout / "blobs/sha256"
    blobs.mkdir(parents=True)

    def blob(payload, media_type):
        identity = vex.digest(payload)
        (blobs / identity).write_bytes(payload)
        return {
            "digest": "sha256:" + identity,
            "size": len(payload),
            "mediaType": media_type,
        }

    with tarfile.open(image) as archive:
        layer = archive.extractfile("layer.tar").read()
    config_descriptor = blob(
        encoded(raw["Metadata"]["ImageConfig"]),
        "application/vnd.oci.image.config.v1+json",
    )
    layer_descriptor = blob(
        gzip.compress(layer), "application/vnd.oci.image.layer.v1.tar+gzip"
    )
    descriptor = blob(
        encoded(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": config_descriptor,
                "layers": [layer_descriptor],
            }
        ),
        "application/vnd.oci.image.manifest.v1+json",
    )
    descriptor["platform"] = {"os": "linux", "architecture": arch}
    if arch == "arm64":
        descriptor["platform"]["variant"] = "v8"
    (layout / "index.json").write_bytes(
        encoded(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "manifests": [descriptor],
            }
        )
    )
    output = evidence[0] / "vex.json"
    vex.prepare(layout, report, source_root, SHA, arch, output)
    assert output.exists()
    output.unlink()
    (blobs / config_descriptor["digest"][7:]).write_bytes(b"tampered config")
    with pytest.raises(vex.VexError):
        vex.prepare(layout, report, source_root, SHA, arch, output)
    assert not output.exists()


def test_evidence_ancestor_cannot_be_a_symlink():
    layer = layer_bytes(
        {"usr/bin": (tarfile.SYMTYPE, "elsewhere"), "usr/bin/git": b"git"}
    )
    with pytest.raises(vex.VexError, match="ancestor"):
        vex.filesystem([lambda: io.BytesIO(layer)], ["sha256:" + vex.digest(layer)])


def test_uncompressed_layer_budget_is_enforced(monkeypatch):
    layer = layer_bytes({"a": b"x" * 2048})
    monkeypatch.setattr(vex, "MAX_BYTES", 1024)
    with pytest.raises(vex.VexError, match="budget"):
        vex.filesystem([lambda: io.BytesIO(layer)], ["sha256:" + vex.digest(layer)])


def test_raw_evidence_log_envelope_preserves_exact_bytes(tmp_path, capsys):
    raw = b'{"raw": true}\n'
    (tmp_path / "raw.json").write_bytes(raw)
    vex.retain(tmp_path)
    lines = capsys.readouterr().out.splitlines()
    payload = zlib.decompress(base64.b64decode("".join(lines[1:-1])))
    assert vex.digest(payload) == lines[0].split("sha256=")[1].split()[0]
    files = json.loads(payload)["files"]
    assert len(files) == 1
    assert base64.b64decode(files[0]["bytes_base64"]) == raw
    assert files[0]["sha256"] == vex.digest(raw)


def test_evidence_retention_rejects_unexpected_files(tmp_path):
    (tmp_path / "unexpected.json").write_bytes(b"{}")
    with pytest.raises(vex.VexError):
        vex.retain(tmp_path)


@pytest.mark.parametrize(
    "relative", [".github/workflows/ci.yml", ".github/workflows/publish-container.yml"]
)
def test_workflow_binds_preparation_and_final_accounting_around_scoped_scan(relative):
    import yaml

    workflow = yaml.safe_load((ROOT / relative).read_text(encoding="utf-8"))
    blocks = [
        step["run"]
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if "scan=(" in step.get("run", "")
    ]
    assert len(blocks) == 1
    block = blocks[0]
    assert block.index('"${scan[@]}" --format json') < block.index(
        "--ignore-unfixed --exit-code 1"
    )
    assert block.index("--ignore-unfixed --exit-code 1") < block.index(
        "prepare_git_vex.py prepare"
    )
    assert block.index("prepare_git_vex.py prepare") < block.index("--vex ")
    assert block.index("--vex ") < block.index("prepare_git_vex.py check")
    assert (
        block.count("prepare_git_vex.py prepare")
        == block.count("prepare_git_vex.py check")
        == 1
    )
    assert "--config /dev/null" in block
    assert "prepare_git_vex.py retain" in block and " EXIT" in block
    if "publish-container" in relative:
        assert '--source-root "$GITHUB_WORKSPACE" --release-sha "$RELEASE_SHA"' in block
        assert '--image "$RUNNER_TEMP/trivy-oci-scan/layout"' in block
    else:
        assert '--source-root "$GITHUB_WORKSPACE" --release-sha "$GITHUB_SHA"' in block
        assert block.count('--image "$archive"') == 2


def read_layers(layers):
    return vex.filesystem(
        [lambda data=data: io.BytesIO(data) for data in layers],
        ["sha256:" + vex.digest(data) for data in layers],
    )


@pytest.mark.parametrize("recreate_directory", [False, True])
def test_hardlink_cannot_reuse_stale_good_child_below_replaced_directory(
    recreate_directory,
):
    directory = (tarfile.DIRTYPE, "")
    layers = [
        layer_bytes(
            {
                "tmp": directory,
                "tmp/source": directory,
                "tmp/source/git": b"GOOD",
                "tmp/evil": directory,
                "tmp/evil/git": b"BAD",
            }
        ),
        layer_bytes({"tmp/source": (tarfile.SYMTYPE, "evil")}),
    ]
    if recreate_directory:
        layers.append(layer_bytes({"tmp/source": directory}))
    layers.append(
        layer_bytes(
            {
                "usr": directory,
                "usr/bin": directory,
                "usr/bin/git": (tarfile.LNKTYPE, "tmp/source/git"),
            }
        )
    )
    with pytest.raises(vex.VexError):
        read_layers(layers)


def test_pax_sparse_expansion_is_rejected_before_logical_payload_read(monkeypatch):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path in ("sparse-a", "sparse-b"):
            member = tarfile.TarInfo(path)
            member.size = 1
            member.pax_headers = {"GNU.sparse.map": "0,1", "GNU.sparse.size": "65536"}
            archive.addfile(member, io.BytesIO(b"X"))
    layer = output.getvalue()
    assert len(layer) == 10240
    monkeypatch.setattr(vex, "MAX_BYTES", 65536)
    with pytest.raises(vex.VexError, match="sparse"):
        read_layers([layer])


@pytest.mark.parametrize("opaque", [False, True])
@pytest.mark.parametrize("whiteout_first", [False, True])
def test_whiteouts_affect_only_lower_layers_regardless_of_tar_order(
    opaque, whiteout_first
):
    first = layer_bytes({"usr/bin/git": b"OLD", "usr/bin/lower-only": b"LOWER"})
    whiteout = "usr/bin/.wh..wh..opq" if opaque else "usr/bin/.wh.git"
    entries = [(whiteout, b""), ("usr/bin/git", b"NEW")]
    if not whiteout_first:
        entries.reverse()
    files = read_layers([first, layer_bytes(dict(entries))])
    assert vex.file_record(files, "usr/bin/git")[1] == vex.digest(b"NEW")
    assert ("usr/bin/lower-only" not in files) == opaque


@pytest.mark.parametrize(
    "record", [{"ModifiedFindings": []}, {"ExperimentalModifiedFindings": None}]
)
def test_wrong_suppression_schema_is_not_treated_as_empty(record):
    with pytest.raises(vex.VexError):
        vex.modified_findings(record)


@pytest.mark.parametrize(
    "field", ["release_sha", "architecture", "source_archive_sha256"]
)
def test_vex_receipt_cannot_change_source_or_architecture_after_preparation(
    evidence, field
):
    image, report, raw, source_root = archive_and_report(evidence)
    output = evidence[0] / "vex.json"
    vex.prepare(image, report, source_root, SHA, "amd64", output)
    document = json.loads(output.read_bytes())
    document["boundver_image_binding"][field] = "tampered"
    output.write_bytes(encoded(document))
    with pytest.raises(vex.VexError, match="binding"):
        vex.verify_scoped(image, report, report, output)
