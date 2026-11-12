#!/usr/bin/env python3
"""Prepare an ephemeral Git-only VEX after checking exact offline image bytes.

Offline Trivy has no root image PURL. Its VEX match is therefore component-only;
the caller MUST pass the same immutable input to this helper and the subsequent
scan, verify the binding receipt, and never reuse the VEX for another image.
Nothing is executed from the image. This is not a general VEX policy engine.
"""

from __future__ import annotations

import argparse
import base64
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import gzip
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tarfile
import zlib
from urllib.parse import parse_qs, unquote


MAX_BYTES = 2 * 1024**3
MAX_MEMBERS = 200_000
MAX_JSON = 16 * 1024**2
MAX_WHITEOUT_WORK = 20_000_000
SOURCE = "usr/share/licenses/git/source/"
CVES = ("CVE-2026-105437", "CVE-2026-111760", "CVE-2026-114466")
RECIPE = (
    "Dockerfile",
    "LICENSE",
    "scripts/minimal_git/README.md",
    "scripts/minimal_git/audit.py",
    "scripts/minimal_git/build.sh",
    "scripts/minimal_git/snapshot.sh",
    "scripts/minimal_git/git-release-key.gpg",
)
SHA = re.compile(r"[0-9a-f]{64}")
KEPT_FILES = {
    SOURCE + entry
    for entry in (
        "build.json",
        "upstream.json",
        "git-2.50.2.tar.xz",
        "git-2.50.2.tar.sign",
        "git-release-key.gpg",
        *("recipe/" + entry for entry in RECIPE),
    )
} | {"var/lib/dpkg/status"}


class VexError(ValueError):
    """Missing, changed or ambiguous evidence; no VEX may be emitted."""


def require(condition, message):
    if not condition:
        raise VexError(message)


def object_pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def json_value(raw):
    require(len(raw) <= MAX_JSON, "JSON exceeds evidence budget")

    def integer(value):
        require(len(value) <= 20, "JSON integer exceeds evidence budget")
        return int(value)

    def number(value):
        require(len(value) <= 64 and math.isfinite(float(value)), "invalid JSON number")
        return float(value)

    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=object_pairs,
        parse_int=integer,
        parse_float=number,
        parse_constant=lambda _: (_ for _ in ()).throw(VexError("non-finite JSON")),
    )


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def modified_findings(result):
    # This is the actual JSON field emitted by the pinned Trivy 0.73.0 writer.
    # A guessed/legacy spelling must not conceal suppression in a raw report.
    require("ModifiedFindings" not in result, "unsupported modified-finding schema")
    value = result.get("ExperimentalModifiedFindings", [])
    require(isinstance(value, list), "invalid modified-finding list")
    return value


def stream_digest(stream):
    h = hashlib.sha256()
    while block := stream.read(1024**2):
        h.update(block)
    return h.hexdigest()


def input_digest(path):
    require(not path.is_symlink(), "symlink image input")
    if not path.is_dir():
        require(
            not path.is_symlink()
            and path.is_file()
            and path.stat().st_size <= MAX_BYTES,
            "unsafe input fingerprint target",
        )
        with path.open("rb") as stream:
            return stream_digest(stream)
    h = hashlib.sha256()
    total = 0
    count = 0
    entries = []
    for entry in path.rglob("*"):
        require(len(entries) < 8192, "OCI directory entry budget exceeded")
        entries.append(entry)
    for entry in sorted(entries):
        require(not entry.is_symlink(), "symlink in OCI input")
        if entry.is_dir():
            continue
        require(
            stat.S_ISREG(entry.stat(follow_symlinks=False).st_mode),
            "non-regular OCI input",
        )
        count += 1
        total += entry.stat().st_size
        require(
            count <= 4096 and total <= MAX_BYTES,
            "OCI input fingerprint budget exceeded",
        )
        relative = entry.relative_to(path).as_posix().encode("utf-8")
        h.update(len(relative).to_bytes(4, "big") + relative)
        with entry.open("rb") as stream:
            h.update(bytes.fromhex(stream_digest(stream)))
    return h.hexdigest()


def name(value):
    require(isinstance(value, str) and len(value) <= 4096, "invalid image path")
    while value.startswith("./"):
        value = value[2:]
    value = value.rstrip("/")
    require(
        value
        and not value.startswith("/")
        and "\\" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/"))
        and all(ord(c) >= 32 and ord(c) != 127 for c in value),
        "unsafe image path",
    )
    return value


def bounded_bytes(stream, limit=MAX_JSON):
    raw = stream.read(limit + 1)
    require(len(raw) <= limit, "evidence file exceeds budget")
    return raw


class LayerReader:
    def __init__(self, stream, budget):
        self.stream, self.budget, self.hash = stream, budget, hashlib.sha256()

    def read(self, size=-1):
        size = min(size if size >= 0 else 1024**2, self.budget[0] + 1)
        raw = self.stream.read(size)
        self.budget[0] -= len(raw)
        require(self.budget[0] >= 0, "uncompressed layer budget exceeded")
        self.hash.update(raw)
        return raw


@contextmanager
def image_input(path, arch):
    """Yield config, ordered layer openers and a content-addressed identity."""
    if path.is_dir():

        def blob(descriptor):
            identity = descriptor.get("digest", "")
            require(
                identity.startswith("sha256:") and SHA.fullmatch(identity[7:]),
                "invalid OCI descriptor digest",
            )
            target = path / "blobs/sha256" / identity[7:]
            require(not target.is_symlink() and target.is_file(), "unsafe OCI blob")
            require(
                target.stat().st_size == descriptor.get("size") <= MAX_BYTES,
                "OCI descriptor size mismatch",
            )
            with target.open("rb") as stream:
                actual = stream_digest(stream)
            require(actual == identity[7:], "OCI blob digest mismatch")
            return target

        index = json_value(bounded_bytes((path / "index.json").open("rb")))
        require(
            index.get("schemaVersion") == 2
            and index.get("mediaType") == "application/vnd.oci.image.index.v1+json",
            "invalid OCI selector schema",
        )
        descriptors = index.get("manifests", [])
        require(len(descriptors) == 1, "OCI selector must select exactly one image")
        selected = descriptors[0]
        require(
            selected.get("mediaType") == "application/vnd.oci.image.manifest.v1+json",
            "not an OCI image manifest",
        )
        platform = selected.get("platform", {})
        require(
            platform.get("architecture") == arch
            and platform.get("os") == "linux"
            and set(platform) <= {"architecture", "os", "variant"}
            and platform.get("variant")
            in ({None, "v8"} if arch == "arm64" else {None}),
            "OCI selector platform mismatch",
        )
        manifest = json_value(bounded_bytes(blob(selected).open("rb")))
        require(
            manifest.get("schemaVersion") == 2
            and manifest.get("mediaType") == selected["mediaType"],
            "invalid OCI manifest schema",
        )
        require(
            manifest["config"].get("mediaType")
            == "application/vnd.oci.image.config.v1+json",
            "invalid OCI image config media type",
        )
        require(
            isinstance(manifest["layers"], list)
            and 0 < len(manifest["layers"]) <= 64
            and all(
                layer.get("mediaType")
                in {
                    "application/vnd.oci.image.layer.v1.tar",
                    "application/vnd.oci.image.layer.v1.tar+gzip",
                }
                for layer in manifest["layers"]
            ),
            "unsupported OCI layers",
        )
        config = json_value(bounded_bytes(blob(manifest["config"]).open("rb")))
        layers = [blob(item) for item in manifest["layers"]]
        yield (
            config,
            [lambda p=p: p.open("rb") for p in layers],
            manifest["config"]["digest"],
        )
        return
    require(
        not path.is_symlink() and path.is_file() and path.stat().st_size <= MAX_BYTES,
        "unsafe image archive",
    )
    with tarfile.open(path, "r:") as archive:
        members = {}
        total = 0
        for member in archive:
            require(len(members) < MAX_MEMBERS, "archive member budget exceeded")
            entry = name(member.name)
            require(entry not in members, "duplicate archive member")
            require(member.isfile() or member.isdir(), "unsupported archive member")
            total += member.size
            require(
                0 <= member.size <= MAX_BYTES and total <= MAX_BYTES,
                "archive content budget exceeded",
            )
            members[entry] = member

        def read_entry(entry, limit=MAX_JSON):
            require(
                entry in members and members[entry].isfile(), "missing archive file"
            )
            with archive.extractfile(members[entry]) as stream:
                return bounded_bytes(stream, limit)

        manifests = json_value(read_entry("manifest.json"))
        require(
            isinstance(manifests, list) and len(manifests) == 1,
            "archive must contain exactly one image",
        )
        manifest = manifests[0]
        config_name = name(manifest["Config"])
        config_bytes = read_entry(config_name)
        config_hash = digest(config_bytes)
        require(
            config_name in {config_hash + ".json", "blobs/sha256/" + config_hash},
            "archive config digest mismatch",
        )
        layer_names = [name(item) for item in manifest["Layers"]]
        require(len(layer_names) == len(set(layer_names)), "duplicate image layer")
        for entry in layer_names:
            require(entry in members and members[entry].isfile(), "missing image layer")
        yield (
            json_value(config_bytes),
            [
                lambda entry=entry: archive.extractfile(members[entry])
                for entry in layer_names
            ],
            "sha256:" + config_hash,
        )


def filesystem(layers, diff_ids):
    require(0 < len(layers) == len(diff_ids) <= 64, "invalid layer inventory")
    files = {}
    budget = [MAX_BYTES]
    logical_left = MAX_BYTES
    members_left = MAX_MEMBERS
    transition_work = 0
    for opener, expected in zip(layers, diff_ids):
        operations = []
        whiteouts = []
        with opener() as compressed:
            prefix = compressed.read(2)
            compressed.seek(0)
            stream = (
                gzip.GzipFile(fileobj=compressed)
                if prefix == b"\x1f\x8b"
                else compressed
            )
            reader = LayerReader(stream, budget)
            with tarfile.open(fileobj=reader, mode="r|") as layer:
                seen = set()
                for member in layer:
                    members_left -= 1
                    require(members_left >= 0, "layer member budget exceeded")
                    # Tar root-directory records carry no payload or filesystem change.
                    if member.isdir() and member.name in {".", "./", "/"}:
                        continue
                    entry = name(member.name)
                    require(entry not in seen, "duplicate layer member")
                    seen.add(entry)
                    require(
                        member.sparse is None
                        and member.type != tarfile.GNUTYPE_SPARSE
                        and not any(
                            key.startswith("GNU.sparse.") for key in member.pax_headers
                        ),
                        "sparse image members are unsupported",
                    )
                    parent, _, leaf = entry.rpartition("/")
                    if leaf.startswith(".wh."):
                        require(member.size == 0, "whiteout has unexpected payload")
                        whiteouts.append((parent, leaf))
                        continue
                    if member.isfile():
                        require(
                            0 <= member.size <= MAX_BYTES, "unsafe layer member size"
                        )
                        logical_left -= member.size
                        require(logical_left >= 0, "logical payload budget exceeded")
                        h = hashlib.sha256()
                        saved = [] if entry in KEPT_FILES else None
                        require(
                            saved is None or member.size <= MAX_JSON,
                            "source evidence too large",
                        )
                        with layer.extractfile(member) as payload:
                            while block := payload.read(1024**2):
                                h.update(block)
                                if saved is not None:
                                    saved.append(block)
                        record = (
                            "file",
                            h.hexdigest(),
                            b"".join(saved) if saved is not None else None,
                        )
                    elif member.islnk():
                        record = ("hardlink", name(member.linkname), None)
                    else:
                        record = (
                            "directory" if member.isdir() else "other",
                            "",
                            None,
                        )
                    operations.append((entry, record))
            while reader.read(1024**2):
                pass
            require(
                "sha256:" + reader.hash.hexdigest() == expected,
                "image layer diffID mismatch",
            )
            if stream is not compressed:
                stream.close()
        # Never resolve a marker through a lower-layer symlink or an ancestor
        # replaced by a non-directory in this layer. Runtimes may follow that
        # parent before removing the actual target, invalidating cached hashes.
        non_directories = {
            entry for entry, record in operations if record[0] != "directory"
        }
        for parent, _ in whiteouts:
            parts = parent.split("/") if parent else []
            for index in range(1, len(parts) + 1):
                ancestor = "/".join(parts[:index])
                require(
                    (ancestor not in files or files[ancestor][0] == "directory")
                    and ancestor not in non_directories,
                    "non-directory whiteout ancestor",
                )
        # OCI whiteouts affect only lower layers, regardless of tar member order.
        for parent, leaf in whiteouts:
            transition_work += len(files)
            require(
                transition_work <= MAX_WHITEOUT_WORK, "whiteout work budget exceeded"
            )
            opaque = leaf == ".wh..wh..opq"
            target = parent if opaque else ((parent + "/" if parent else "") + leaf[4:])
            for prior in list(files):
                if (
                    (opaque and not parent)
                    or (not opaque and prior == target)
                    or prior.startswith(target + "/")
                ):
                    del files[prior]
        for entry, record in operations:
            parts = entry.split("/")
            for index in range(1, len(parts)):
                parent = "/".join(parts[:index])
                require(
                    parent not in files or files[parent][0] == "directory",
                    "non-directory image ancestor",
                )
                files.setdefault(parent, ("directory", "", None))
            previous = files.get(entry)
            if previous and (previous[0] == "directory") != (record[0] == "directory"):
                transition_work += len(files)
                require(
                    transition_work <= MAX_WHITEOUT_WORK,
                    "directory replacement work budget exceeded",
                )
                for prior in list(files):
                    if prior.startswith(entry + "/"):
                        del files[prior]
            if record[0] == "hardlink":
                # A cached child below a replaced/symlink directory is not the
                # inode that a real hardlink would resolve to.
                record = file_record(files, record[1])
            files[entry] = record
    return files


def file_record(files, path):
    require(
        path in files and files[path][0] == "file",
        "missing regular image evidence: " + path,
    )
    parts = path.split("/")
    for index in range(1, len(parts)):
        parent = "/".join(parts[:index])
        require(
            parent not in files or files[parent][0] == "directory",
            "non-directory image ancestor",
        )
    return files[path]


def source_audit():
    location = Path(__file__).with_name("minimal_git") / "audit.py"
    spec = importlib.util.spec_from_file_location("boundver_git_vex_source", location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git_environment():
    location = Path(__file__).with_name("_release_platform.py")
    spec = importlib.util.spec_from_file_location("boundver_git_vex_platform", location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    environment = module.sanitize_git_environment(os.environ)
    environment.update(
        GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1", GIT_OPTIONAL_LOCKS="0"
    )
    return environment


def validate_payload(files, arch, source_root):
    trusted = source_audit()
    build = json_value(file_record(files, SOURCE + "build.json")[2])
    require(build.get("architecture") == arch, "Git build architecture mismatch")
    require(
        build.get("source_version") == "1:2.50.2"
        and build.get("binary_version") == "1:2.50.2+boundver1",
        "unreviewed Git version",
    )
    require(
        build.get("upstream_authentication") == trusted.source_metadata("1:2.50.2"),
        "upstream authentication record mismatch",
    )
    require(
        build.get("source_components")
        == {key: value[1] for key, value in trusted.SOURCE_FILES.items()},
        "source component inventory mismatch",
    )
    for entry, (size, expected) in trusted.SOURCE_FILES.items():
        record = file_record(files, SOURCE + entry)
        require(
            record[1] == expected and len(record[2]) == size,
            "upstream source bytes mismatch",
        )
    require(
        json_value(file_record(files, SOURCE + "upstream.json")[2])
        == trusted.source_metadata("1:2.50.2"),
        "upstream source manifest mismatch",
    )
    for entry in RECIPE:
        local = source_root / entry
        require(
            not local.is_symlink() and local.is_file(), "unsafe trusted recipe input"
        )
        expected = digest(bounded_bytes(local.open("rb")))
        require(
            file_record(files, SOURCE + "recipe/" + entry)[1] == expected,
            "image recipe differs from reviewed release source",
        )
    elfs = build.get("elfs", [])
    require(
        isinstance(elfs, list)
        and len(elfs) == len(trusted.ELFS)
        and {item["path"] for item in elfs} == trusted.ELFS,
        "Git ELF inventory mismatch",
    )
    for item in elfs:
        require(
            file_record(files, item["path"])[1] == item["sha256"],
            "installed Git ELF changed",
        )
    status_text = file_record(files, "var/lib/dpkg/status")[2].decode("utf-8")
    packages = [
        dict(
            line.split(": ", 1)
            for line in stanza.splitlines()
            if ": " in line and not line.startswith(" ")
        )
        for stanza in status_text.split("\n\n")
    ]
    git = [package for package in packages if package.get("Package") == "git"]
    require(
        len(git) == 1
        and git[0].get("Status") == "install ok installed"
        and git[0].get("Version") == "1:2.50.2+boundver1"
        and git[0].get("Source") == "git (1:2.50.2)"
        and git[0].get("Architecture") == arch,
        "installed Git package metadata mismatch",
    )


def prepare(image, report_path, source_root, release_sha, arch, output):
    require(re.fullmatch(r"[0-9a-f]{40}", release_sha), "invalid release SHA")
    require(arch in {"amd64", "arm64"}, "unsupported architecture")
    actual = subprocess.run(
        ["git", "-C", str(source_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        timeout=30,
        encoding="ascii",
        env=git_environment(),
    ).stdout.strip()
    require(actual == release_sha, "trusted source checkout SHA mismatch")
    # Tracked changes could otherwise replace the recipe without changing HEAD.
    subprocess.run(
        [
            "git",
            "-C",
            str(source_root),
            "diff",
            "--quiet",
            "--no-ext-diff",
            "--no-textconv",
            "HEAD",
            "--",
            *RECIPE,
        ],
        check=True,
        capture_output=True,
        timeout=30,
        env=git_environment(),
    )
    report = json_value(bounded_bytes(report_path.open("rb")))
    require(
        report.get("SchemaVersion") == 2
        and report.get("ArtifactType") == "container_image",
        "not a raw container scan",
    )
    require(
        report.get("Trivy", {}).get("Version") == "0.73.0", "unreviewed scanner version"
    )
    require(
        all(not modified_findings(result) for result in report.get("Results", [])),
        "raw report already contains suppressed findings",
    )
    before_digest = input_digest(image)
    with image_input(image, arch) as (config, layers, image_id):
        require(
            config.get("os") == "linux" and config.get("architecture") == arch,
            "image architecture mismatch",
        )
        require(
            config.get("config", {})
            .get("Labels", {})
            .get("org.opencontainers.image.revision")
            == release_sha,
            "image release revision mismatch",
        )
        metadata = report.get("Metadata", {})
        require(
            metadata.get("ImageID") == image_id
            and metadata.get("ImageConfig") == config,
            "scan is not bound to this exact image config",
        )
        require(
            metadata.get("DiffIDs") == config["rootfs"]["diff_ids"],
            "scan layer identity mismatch",
        )
        validate_payload(
            filesystem(layers, config["rootfs"]["diff_ids"]), arch, source_root
        )
    os_data = report.get("Metadata", {}).get("OS", {})
    require(
        os_data.get("Family") == "debian"
        and re.fullmatch(r"13(?:\.[0-9]{1,3})?", os_data.get("Name", "")),
        "unexpected scanned distribution",
    )
    expected_purl = (
        "pkg:deb/debian/git@2.50.2%2Bboundver1?arch="
        + arch
        + "&distro=debian-"
        + os_data["Name"]
        + "&epoch=1"
    )
    for result in report.get("Results", []):
        for finding in result.get("Vulnerabilities", []) or []:
            if (
                finding.get("VulnerabilityID") not in CVES
                or finding.get("PkgName") != "git"
            ):
                continue
            require(
                result.get("Class") == "os-pkgs"
                and result.get("Type") == "debian"
                and finding.get("InstalledVersion") == "1:2.50.2+boundver1"
                and finding.get("DataSource", {}).get("ID") == "debian"
                and not finding.get("FixedVersion"),
                "Git finding is outside authorized correction",
            )
            purl = finding.get("PkgIdentifier", {}).get("PURL", "")
            base, _, query = purl.partition("?")
            qualifiers = parse_qs(query, strict_parsing=True)
            require(
                unquote(base) == "pkg:deb/debian/git@2.50.2+boundver1"
                and qualifiers.get("arch") == [arch]
                and qualifiers.get("epoch") == ["1"]
                and set(qualifiers) == {"arch", "epoch", "distro"},
                "Git package PURL mismatch",
            )
            require(purl == expected_purl, "noncanonical Git package PURL")
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    require(
        input_digest(image) == before_digest, "image input changed during verification"
    )
    binding = {
        "image_config_digest": image_id,
        "release_sha": release_sha,
        "architecture": arch,
        "raw_report_sha256": digest(report_path.read_bytes()),
        "input_sha256": before_digest,
        "source_archive_sha256": source_audit().SOURCE_FILES["git-2.50.2.tar.xz"][1],
    }
    vex = {
        "@context": "https://openvex.dev/ns/v0.2.0",
        "@id": "https://github.com/yzm1/boundver/vex/"
        + digest(json.dumps(binding, sort_keys=True).encode()),
        "author": "https://github.com/yzm1/boundver",
        "timestamp": timestamp,
        "version": 1,
        "boundver_image_binding": binding,
        "statements": [
            {
                "vulnerability": {"name": cve},
                "status": "fixed",
                "products": [{"@id": purl}],
                "action_statement": "Upstream Git 2.50.2 security fixes; source bundle and installed ELF hashes verified for this image only.",
            }
            for cve in CVES
            for purl in [expected_purl]
        ],
    }
    with output.open("xb") as stream:
        stream.write((json.dumps(vex, sort_keys=True, indent=2) + "\n").encode("utf-8"))
    print(
        f"Prepared Git-only VEX for {image_id}, linux/{arch}, {release_sha}; {len(vex['statements'])} fixed statements."
    )


def verify_scoped(image, raw_path, scoped_path, vex_path):
    raw_bytes = bounded_bytes(raw_path.open("rb"))
    raw = json_value(raw_bytes)
    scoped = json_value(bounded_bytes(scoped_path.open("rb")))
    vex = json_value(bounded_bytes(vex_path.open("rb")))
    binding = vex["boundver_image_binding"]
    config = raw.get("Metadata", {}).get("ImageConfig", {})
    require(
        binding.get("release_sha")
        == config.get("config", {})
        .get("Labels", {})
        .get("org.opencontainers.image.revision")
        and binding.get("architecture") == config.get("architecture")
        and binding.get("source_archive_sha256")
        == source_audit().SOURCE_FILES["git-2.50.2.tar.xz"][1],
        "VEX source/revision/architecture binding changed",
    )
    require(
        binding["raw_report_sha256"] == digest(raw_bytes)
        and binding["input_sha256"] == input_digest(image),
        "VEX image/report binding changed",
    )
    require(
        raw.get("Metadata") == scoped.get("Metadata")
        and raw.get("Metadata", {}).get("ImageID") == binding["image_config_digest"],
        "scoped scan changed image identity",
    )
    statements = vex.get("statements", [])
    require(
        len(statements) == 3
        and {s["vulnerability"]["name"] for s in statements} == set(CVES),
        "VEX statement scope changed",
    )
    products = {s["products"][0]["@id"] for s in statements}
    require(
        len(products) == 1
        and all(s["status"] == "fixed" and len(s["products"]) == 1 for s in statements),
        "VEX product scope changed",
    )
    purl = next(iter(products))
    expected = Counter()
    accounted = Counter()

    def key(result, finding):
        return (
            result.get("Class"),
            result.get("Type"),
            result.get("Target"),
            finding.get("VulnerabilityID"),
            finding.get("PkgName"),
            finding.get("InstalledVersion"),
            finding.get("PkgIdentifier", {}).get("PURL"),
            finding.get("Layer", {}).get("DiffID"),
            finding.get("Severity"),
        )

    for result in raw.get("Results", []):
        require(not modified_findings(result), "raw scan contains suppressed findings")
        expected.update(
            key(result, finding) for finding in result.get("Vulnerabilities", []) or []
        )
    # Existing residual-risk policy is independent of the new fixed assessment.
    policy = Path(__file__).resolve().parents[1] / ".trivyignore.yaml"
    accepted = set()
    current = None
    for line in (
        bounded_bytes(policy.open("rb"), 64 * 1024).decode("utf-8").splitlines()
    ):
        if match := re.fullmatch(r"  - id: (CVE-[0-9]{4}-[0-9]+)", line):
            current = match[1]
        if match := re.fullmatch(r"      - (pkg:deb/debian/[A-Za-z0-9.+-]+)", line):
            accepted.add((current, match[1]))
    for result in scoped.get("Results", []):
        require(
            not result.get("Vulnerabilities"),
            "scoped scan retains HIGH/CRITICAL findings",
        )
        for modified in modified_findings(result):
            require(
                modified.get("Type") == "vulnerability",
                "unexpected modified finding type",
            )
            finding = modified["Finding"]
            finding_purl = finding.get("PkgIdentifier", {}).get("PURL", "")
            if modified.get("Status") == "fixed":
                require(
                    finding.get("VulnerabilityID") in CVES
                    and finding.get("PkgName") == "git"
                    and finding.get("InstalledVersion") == "1:2.50.2+boundver1"
                    and finding_purl == purl,
                    "VEX suppressed an unauthorized finding",
                )
            else:
                require(
                    modified.get("Status") == "ignored"
                    and (finding.get("VulnerabilityID"), finding_purl.partition("@")[0])
                    in accepted,
                    "suppression is outside the existing accepted policy",
                )
            accounted[key(result, finding)] += 1
    require(
        expected == accounted,
        "raw findings are missing, changed or duplicated in scoped report",
    )
    print(
        "Every raw finding is accounted for; only the three authorized Git fixes and existing policy apply."
    )


def retain(reports):
    """Keep exact evidence in job logs without changing release artifact names."""
    entries = []
    total = 0
    for path in sorted(reports.rglob("*.json")):
        relative = path.relative_to(reports).as_posix()
        parts = relative.split("/")
        require(
            len(parts) == 1
            or (len(parts) == 2 and parts[0] in {"linux-amd64", "linux-arm64"}),
            "unexpected evidence directory",
        )
        require(
            parts[-1] in {"raw.json", "fixable.json", "scoped.json", "git-vex.json"}
            and not path.is_symlink()
            and path.is_file(),
            "unexpected evidence file",
        )
        with path.open("rb") as stream:
            raw = bounded_bytes(stream, 4 * 1024**2)
        total += len(raw)
        require(
            total <= 4 * 1024**2 and len(entries) < 8,
            "job-log evidence budget exceeded",
        )
        json_value(raw)
        entries.append(
            {
                "path": relative,
                "sha256": digest(raw),
                "bytes_base64": base64.b64encode(raw).decode("ascii"),
            }
        )
    payload = json.dumps(
        {"schema": "boundver-security-evidence/v1", "files": entries}, sort_keys=True
    ).encode()
    print(
        "BEGIN boundver-security-evidence/v1 sha256="
        + digest(payload)
        + " encoding=zlib+base64"
    )
    encoded = base64.b64encode(zlib.compress(payload, 9)).decode("ascii")
    for offset in range(0, len(encoded), 120):
        print(encoded[offset : offset + 120])
    print("END boundver-security-evidence/v1")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    check_parser = commands.add_parser("check")
    retain_parser = commands.add_parser("retain")
    retain_parser.add_argument("--reports", required=True, type=Path)
    for target in (prepare_parser, check_parser):
        target.add_argument("--image", required=True, type=Path)
        target.add_argument("--raw-report", required=True, type=Path)
    check_parser.add_argument("--scoped-report", required=True, type=Path)
    check_parser.add_argument("--vex", required=True, type=Path)
    prepare_parser.add_argument("--source-root", required=True, type=Path)
    prepare_parser.add_argument("--release-sha", required=True)
    prepare_parser.add_argument(
        "--architecture", required=True, choices=["amd64", "arm64"]
    )
    prepare_parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            prepare(
                args.image,
                args.raw_report,
                args.source_root,
                args.release_sha,
                args.architecture,
                args.output,
            )
        elif args.command == "check":
            verify_scoped(args.image, args.raw_report, args.scoped_report, args.vex)
        else:
            retain(args.reports)
    except (
        VexError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        tarfile.TarError,
        subprocess.SubprocessError,
        RecursionError,
    ) as error:
        print(f"Git VEX preparation failed: {str(error)[:512]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
