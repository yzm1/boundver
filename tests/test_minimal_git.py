"""Fail-closed native build contracts; these run in the default core tier."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import lzma
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "minimal_git_audit", ROOT / "scripts/minimal_git/audit.py"
)
assert SPEC and SPEC.loader
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)

HEADER = """ELF Header:
  Class: ELF64
  Data: 2's complement, little endian
  Type: DYN (Position-Independent Executable file)
  Machine: Advanced Micro Devices X86-64
"""
SEGMENTS = """  GNU_STACK 0x00 0x00 0x00 0x00 0x00 RW 0x10
  GNU_RELRO 0x00 0x00 0x00 0x00 0x00 R 0x1
      [Requesting program interpreter: /lib64/ld-linux-x86-64.so.2]
"""
DYNAMIC = """ 0x1 (NEEDED) Shared library: [libc.so.6]
 0x1 (NEEDED) Shared library: [libpcre2-8.so.0]
 0x1 (NEEDED) Shared library: [libz.so.1]
 0x1e (FLAGS) BIND_NOW
 0x6ffffffb (FLAGS_1) Flags: NOW PIE
"""
DEPS = "shlibs:Depends=libc6 (>= 2.38), libpcre2-8-0 (>= 10.34), zlib1g (>= 1:1.2.2)\n"


@pytest.mark.parametrize("arch", ["amd64", "arm64"])
def test_native_hardened_elf(arch):
    header = HEADER.replace(audit.MACHINES["amd64"], audit.MACHINES[arch])
    segments = SEGMENTS
    if arch == "arm64":
        segments = segments.replace(
            "/lib64/ld-linux-x86-64.so.2", "/lib/ld-linux-aarch64.so.1"
        )
    audit.check_elf(header, segments, DYNAMIC, ".text .data", arch)


@pytest.mark.parametrize("arch", ["amd64", "arm64"])
def test_only_own_required_loader_may_also_be_a_needed_library(arch):
    header = HEADER.replace(audit.MACHINES["amd64"], audit.MACHINES[arch])
    segments = SEGMENTS.replace(audit.LOADERS["amd64"], audit.LOADERS[arch])
    own_loader = Path(audit.LOADERS[arch]).name
    needed = DYNAMIC + f" 0x1 (NEEDED) Shared library: [{own_loader}]\n"
    audit.check_elf(header, segments, needed, ".text", arch)
    other_arch = "amd64" if arch == "arm64" else "arm64"
    for unexpected in (
        Path(audit.LOADERS[other_arch]).name,
        "libcurl.so.4",
        "libatomic.so.1",
    ):
        wrong = DYNAMIC + f" 0x1 (NEEDED) Shared library: [{unexpected}]\n"
        with pytest.raises(ValueError, match="unexpected shared library"):
            audit.check_elf(header, segments, wrong, ".text", arch)


@pytest.mark.parametrize(
    ("field", "old", "new"),
    [
        ("header", "ELF64", "ELF32"),
        ("header", "little endian", "big endian"),
        ("header", "DYN", "EXEC"),
        ("header", "Advanced Micro Devices X86-64", "AArch64"),
        ("segments", "GNU_STACK", "other"),
        ("segments", " RW ", " RWE "),
        ("segments", "GNU_RELRO", "other"),
        ("segments", "/lib64/ld-linux-x86-64.so.2", "/tmp/loader"),
        ("dynamic", "BIND_NOW", "other"),
        ("dynamic", "PIE", "other"),
        ("dynamic", "libz.so.1", "libcurl.so.4"),
    ],
)
def test_elf_regressions_fail_closed(field, old, new):
    values = {"header": HEADER, "segments": SEGMENTS, "dynamic": DYNAMIC}
    values[field] = values[field].replace(old, new)
    # NOW in FLAGS_1 also satisfies immediate binding; remove both for this case.
    if old == "BIND_NOW":
        values[field] = values[field].replace("NOW PIE", "PIE")
    with pytest.raises(ValueError):
        audit.check_elf(**values, sections=".text .data", arch="amd64")


@pytest.mark.parametrize(
    "extra", ["(TEXTREL)", "(FLAGS) TEXTREL BIND_NOW", "(RPATH)", "(RUNPATH)"]
)
def test_relocations_and_custom_search_paths_fail(extra):
    with pytest.raises(ValueError):
        audit.check_elf(HEADER, SEGMENTS, DYNAMIC + extra, ".text", "amd64")


@pytest.mark.parametrize("sections", [".debug_info", ".zdebug_line"])
def test_debug_sections_fail(sections):
    with pytest.raises(ValueError):
        audit.check_elf(HEADER, SEGMENTS, DYNAMIC, sections, "amd64")


def test_dependency_union_preserves_versions_and_adds_shell():
    assert audit.dependencies(DEPS) == "dash, " + DEPS.strip().split("=", 1)[1]


def test_dependency_guard_reports_observed_names_without_allowing_them():
    with pytest.raises(
        ValueError, match="unexpected shared library.*amd64.*libcurl.so.4"
    ):
        audit.check_elf(
            HEADER,
            SEGMENTS,
            DYNAMIC.replace("libz.so.1", "libcurl.so.4"),
            ".text",
            "amd64",
        )
    with pytest.raises(ValueError, match="unexpected package dependency set.*libcurl"):
        audit.dependencies(DEPS.replace("libc6", "libcurl"))


@pytest.mark.parametrize(
    "value",
    [
        "",
        "Depends=libc6",
        DEPS + "shlibs:other=x\n",
        DEPS.replace("libc6 (>= 2.38), ", ""),
        DEPS.replace("libc6", "libcurl3t64-gnutls"),
        DEPS.strip() + ", libc6\n",
        DEPS.replace("libc6", "libc6 | surprise"),
    ],
)
def test_unexpected_dependencies_fail(value):
    with pytest.raises(ValueError):
        audit.dependencies(value)


def test_required_hardening_flags_cannot_silently_disappear():
    flags = [
        "-O2 -fstack-protector-strong -fPIE",
        "-D_FORTIFY_SOURCE=3",
        "-pie -Wl,-z,relro,-z,now",
    ]
    audit.check_flags(*flags)
    for index, value in enumerate(flags):
        for token in value.split():
            changed = flags.copy()
            changed[index] = value.replace(token, "")
            with pytest.raises(ValueError, match="hardening"):
                audit.check_flags(*changed)


@pytest.fixture
def source_inputs(tmp_path, monkeypatch):
    payloads = {
        "git-2.50.2.tar.xz": b"archive",
        "git-2.50.2.tar.sign": b"signature",
        "git-release-key.gpg": b"key",
    }
    monkeypatch.setattr(
        audit,
        "SOURCE_FILES",
        {
            name: (len(data), hashlib.sha256(data).hexdigest())
            for name, data in payloads.items()
        },
    )
    for name, data in payloads.items():
        (tmp_path / name).write_bytes(data)
    (tmp_path / "upstream.json").write_bytes(
        (
            json.dumps(
                audit.source_metadata(audit.UPSTREAM_SOURCE_VERSION),
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
    )
    return tmp_path


def test_every_upstream_source_component_is_checked(source_inputs):
    assert audit.source_components(source_inputs) == {
        name: digest for name, (_, digest) in audit.SOURCE_FILES.items()
    }
    for name in audit.SOURCE_FILES:
        payload = source_inputs / name
        original = payload.read_bytes()
        payload.write_bytes(b"corrupt")
        with pytest.raises(ValueError, match="checksum"):
            audit.source_components(source_inputs)
        payload.write_bytes(original)


@pytest.mark.parametrize(
    "name", ["../git.tar.xz", "/git.tar.xz", "git/part", "-option"]
)
def test_manifest_rejects_unsafe_or_unreviewed_component_names(source_inputs, name):
    manifest = source_inputs / "upstream.json"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("git-2.50.2.tar.xz", name),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="manifest"):
        audit.source_components(source_inputs)


def test_manifest_missing_duplicate_tampered_or_oversized_fields_fail(source_inputs):
    manifest = source_inputs / "upstream.json"
    original = manifest.read_text(encoding="utf-8")
    for changed in (
        "{}",
        original + "\n",
        "x" * 16386,
        original.replace(
            '"source_components": {',
            '"source_components": {},\n "source_components": {',
        ),
        original.replace(audit.PRIMARY_FINGERPRINT, "0" * 40),
    ):
        manifest.write_text(changed, encoding="utf-8")
        with pytest.raises(ValueError, match="manifest"):
            audit.source_components(source_inputs)


@pytest.mark.parametrize(
    "interpreter",
    [
        b"#!/usr/bin/python3\n",
        b"#!/usr/bin/perl\n",
        b"#!/usr/bin/env sh\n",
        b"#!/bin/sh\r\n",
        b"#!/bin/sh",
        b"#!/bin/sh\x0b\n",
    ],
)
def test_unexpected_script_interpreters_fail(tmp_path, interpreter):
    (tmp_path / "helper").write_bytes(interpreter)
    with pytest.raises(ValueError, match="interpreter"):
        audit.audit(tmp_path, "amd64", tmp_path / "result.json")


def test_empty_executable_inventory_fails(tmp_path):
    with pytest.raises(ValueError, match="inventory"):
        audit.audit(tmp_path, "amd64", tmp_path / "result.json")


def test_unknown_executable_format_fails(tmp_path):
    path = tmp_path / "unknown"
    path.write_bytes(b"echo unsafe fallback\n")
    path.chmod(0o755)
    if not path.stat().st_mode & 0o111:
        pytest.skip("platform cannot represent execute permission")
    with pytest.raises(ValueError, match="unrecognized executable"):
        audit.audit(tmp_path, "amd64", tmp_path / "result.json")


def test_unreviewed_source_version_is_refused_before_download(tmp_path, monkeypatch):
    def unexpected(*args):
        pytest.fail("Unreviewed source version reached network")

    monkeypatch.setattr(audit, "download_source", unexpected)
    with pytest.raises(ValueError, match="unreviewed Git source"):
        audit.fetch_source(tmp_path, "1:2.47.3-0+deb13u1")


def test_all_unique_installed_elfs_are_charged_to_dependency_check(
    tmp_path, monkeypatch
):
    for name in audit.ELFS:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x7fELF")
    # Installed builtin aliases are hardlinks, not additional programs.
    os.link(tmp_path / "usr/bin/git", tmp_path / "usr/bin/git-upload-pack")
    shell = tmp_path / "usr/libexec/git-core/shell-helper"
    shell.write_bytes(b"#!/bin/sh\nexit 0\n")
    calls = []

    def run(*args):
        calls.append(args)
        if args[0] == "readelf":
            return {"-hW": HEADER, "-lW": SEGMENTS, "-dW": DYNAMIC, "-SW": ".text"}[
                args[1]
            ]
        assert args[0] == "dpkg-shlibdeps"
        return DEPS

    monkeypatch.setattr(audit, "run", run)
    output = tmp_path / "record.json"
    audit.audit(tmp_path, "amd64", output)
    record = json.loads(output.read_text(encoding="utf-8"))
    assert {entry["path"] for entry in record["elfs"]} == audit.ELFS
    assert record["shell_scripts"] == [shell.relative_to(tmp_path).as_posix()]
    dep_calls = [call for call in calls if call[0] == "dpkg-shlibdeps"]
    assert len(dep_calls) == 1
    assert set(dep_calls[0][2:]) == {"-e" + str(tmp_path / name) for name in audit.ELFS}
    # New, unreviewed executable inventory must fail, even if it links safely.
    (tmp_path / "usr/bin/surprise").write_bytes(b"\x7fELF")
    with pytest.raises(ValueError, match="inventory"):
        audit.audit(tmp_path, "amd64", output)


def test_build_recipe_keeps_all_checks_and_source_identity():
    recipe = (ROOT / "scripts/minimal_git/build.sh").read_text(encoding="utf-8")
    for contract in (
        "for variant in a b",
        "cmp /out/git-a.deb /out/git-b.deb",
        "fetch-source /source",
        "tar -xJf /source/git-2.50.2.tar.xz",
        "git version 2.50.2",
        "sha1-is-sha1dc",
        "shattered-1.pdf",
        "hardening=+all reproducible=+all",
        "dpkg-buildflags --get CPPFLAGS",
        "dpkg-buildflags --get CFLAGS",
        "dpkg-buildflags --get LDFLAGS",
        "-fPIE",
        "-pie -Wl,-z,relro,-z,now",
        "NO_CURL=YesPlease",
        "NO_OPENSSL=YesPlease",
        "NO_PERL=YesPlease",
    ):
        assert contract in recipe
    assert "--allow-unauthenticated" not in recipe


def signature_status():
    return (
        f"[GNUPG:] GOODSIG {audit.SIGNER_FINGERPRINT[-16:]} Junio C Hamano\n"
        f"[GNUPG:] VALIDSIG {audit.SIGNER_FINGERPRINT} 2026-11-10 1794259170 0 4 0 1 10 00 {audit.PRIMARY_FINGERPRINT}\n"
    )


def test_valid_signature_binds_primary_and_signing_subkey():
    audit.check_signature_status(signature_status())


@pytest.mark.parametrize(
    "changed",
    [
        "",
        "[GNUPG:] GOODSIG arbitrary\n",
        "duplicate",
        "wrong-primary",
        "wrong-signer",
        "BADSIG",
        "EXPKEYSIG",
        "REVKEYSIG",
        "EXPSIG",
        "ERRSIG",
        "NO_PUBKEY",
        "NODATA",
        "FAILURE",
        "ERROR",
    ],
)
def test_invalid_signature_statuses_fail(changed):
    status = signature_status()
    if changed == "duplicate":
        status += status
    elif changed == "wrong-primary":
        status = status.replace(audit.PRIMARY_FINGERPRINT, "0" * 40)
    elif changed == "wrong-signer":
        status = status.replace(audit.SIGNER_FINGERPRINT, "0" * 40)
    elif changed in {
        "BADSIG",
        "EXPKEYSIG",
        "REVKEYSIG",
        "EXPSIG",
        "ERRSIG",
        "NO_PUBKEY",
        "NODATA",
        "FAILURE",
        "ERROR",
    }:
        status += "[GNUPG:] " + changed + " bad\n"
    else:
        status = changed
    with pytest.raises(ValueError, match="signature"):
        audit.check_signature_status(status)


@pytest.mark.parametrize(
    "url",
    [
        "http://www.kernel.org/pub/software/scm/git/git-2.50.2.tar.xz",
        "https://evil.test/pub/software/scm/git/git-2.50.2.tar.xz",
        "https://www.kernel.org:444/pub/software/scm/git/git-2.50.2.tar.xz",
        "https://user@www.kernel.org/pub/software/scm/git/git-2.50.2.tar.xz",
        "https://www.kernel.org/pub/software/scm/git/git-2.50.1.tar.xz",
        "https://www.kernel.org/pub/software/scm/git/git-2.50.2.tar.xz?key=secret",
        "https://www.kernel.org/pub/software/scm/git/git-2.50.2.tar.xz#fragment",
        "file:///tmp/source",
        "https://127.0.0.1/source",
    ],
)
def test_source_urls_cannot_redirect_to_other_inputs_or_hosts(url):
    with pytest.raises(ValueError):
        audit.check_source_url(url)
    with pytest.raises(ValueError):
        audit.SourceRedirects().redirect_request(None, None, 302, "", {}, url)


def test_source_redirect_count_is_bounded():
    url = "https://www.kernel.org/pub/software/scm/git/git-2.50.2.tar.xz"
    redirects = audit.SourceRedirects()
    request = Request(url)
    redirects.redirect_request(request, None, 302, "", {}, url)
    redirects.redirect_request(request, None, 302, "", {}, url)
    with pytest.raises(ValueError, match="redirect budget"):
        redirects.redirect_request(request, None, 302, "", {}, url)


@pytest.mark.parametrize(
    "response_bytes", [b"source", b"too-large", b"short", b"tamper"]
)
def test_source_download_checks_exact_size_and_digest(
    tmp_path, monkeypatch, response_bytes
):
    name = "git-2.50.2.tar.sign"
    monkeypatch.setitem(
        audit.SOURCE_FILES, name, (6, hashlib.sha256(b"source").hexdigest())
    )

    class Response(io.BytesIO):
        def geturl(self):
            return "https://www.kernel.org/pub/software/scm/git/" + name

    monkeypatch.setattr(
        audit,
        "build_opener",
        lambda *args: SimpleNamespace(
            open=lambda *args, **kwargs: Response(response_bytes),
        ),
    )
    if response_bytes == b"source":
        audit.download_source(tmp_path, name)
        assert (tmp_path / name).read_bytes() == b"source"
    else:
        with pytest.raises(ValueError):
            audit.download_source(tmp_path, name)


@pytest.mark.parametrize("fault", [None, "signature-exit", "wrong-signer", "wrong-tar"])
def test_fetch_authenticates_uncompressed_tar_before_recording_provenance(
    tmp_path, monkeypatch, fault
):
    recipe = tmp_path / "recipe"
    source = tmp_path / "source"
    recipe.mkdir()
    source.mkdir()
    payloads = {
        "git-2.50.2.tar.xz": lzma.compress(
            b"wrong tar" if fault == "wrong-tar" else b"tar bytes"
        ),
        "git-2.50.2.tar.sign": b"signature",
        "git-release-key.gpg": b"public key",
    }
    (recipe / "git-release-key.gpg").write_bytes(payloads["git-release-key.gpg"])
    monkeypatch.setattr(audit, "__file__", str(recipe / "audit.py"))
    monkeypatch.setattr(
        audit,
        "SOURCE_FILES",
        {
            name: (len(data), hashlib.sha256(data).hexdigest())
            for name, data in payloads.items()
        },
    )
    monkeypatch.setattr(audit, "TAR_SIZE", 9)
    monkeypatch.setattr(audit, "TAR_SHA256", hashlib.sha256(b"tar bytes").hexdigest())
    monkeypatch.setattr(
        audit,
        "download_source",
        lambda root, name: (root / name).write_bytes(payloads[name]),
    )
    calls = []

    def verify(argv, **kwargs):
        calls.append((argv, kwargs))
        status = signature_status()
        if fault == "wrong-signer":
            status = status.replace(audit.SIGNER_FINGERPRINT, "0" * 40)
        return SimpleNamespace(
            returncode=1 if fault == "signature-exit" else 0, stdout=status.encode()
        )

    monkeypatch.setattr(audit.subprocess, "run", verify)
    if fault:
        with pytest.raises(ValueError):
            audit.fetch_source(source, audit.UPSTREAM_SOURCE_VERSION)
        assert not (source / "upstream.json").exists()
        if fault == "wrong-tar":
            assert not calls
        return
    audit.fetch_source(source, audit.UPSTREAM_SOURCE_VERSION)
    assert calls[0][0][0] == "/usr/bin/gpgv"
    assert calls[0][1]["input"] == b"tar bytes"
    assert calls[0][1]["timeout"] == 60
    assert calls[0][1]["env"] == {"LC_ALL": "C", "PATH": "/usr/bin:/bin"}
    assert audit.source_components(source) == {
        name: digest for name, (_, digest) in audit.SOURCE_FILES.items()
    }


def test_download_deadline_is_enforced_before_reading(tmp_path, monkeypatch):
    name = "git-2.50.2.tar.sign"
    ticks = iter([0, 91])
    monkeypatch.setattr(audit, "time", SimpleNamespace(monotonic=lambda: next(ticks)))

    class Response(io.BytesIO):
        def geturl(self):
            return "https://www.kernel.org/pub/software/scm/git/" + name

        def read(self, *args):
            pytest.fail("Expired download attempted a read")

    monkeypatch.setattr(
        audit,
        "build_opener",
        lambda *args: SimpleNamespace(
            open=lambda *args, **kwargs: Response(b"signature"),
        ),
    )
    with pytest.raises(ValueError, match="deadline"):
        audit.download_source(tmp_path, name)


def test_vendored_verification_key_matches_the_reviewed_bytes():
    audit.check_source_file(ROOT / "scripts/minimal_git/git-release-key.gpg")


def test_nonempty_source_directory_is_never_reused(tmp_path, monkeypatch):
    (tmp_path / "existing").write_bytes(b"preserve")
    monkeypatch.setattr(
        audit, "download_source", lambda *args: pytest.fail("Unexpected network")
    )
    with pytest.raises(ValueError, match="must be empty"):
        audit.fetch_source(tmp_path, audit.UPSTREAM_SOURCE_VERSION)
    assert (tmp_path / "existing").read_bytes() == b"preserve"


def test_reconstruction_instructions_include_all_root_inputs():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    instructions = (ROOT / "scripts/minimal_git/README.md").read_text(encoding="utf-8")
    assert "COPY Dockerfile LICENSE /recipe/" in dockerfile
    assert "`Dockerfile` and `LICENSE` at its root" in instructions
    assert "`scripts/minimal_git/`" in instructions
