"""Fail-closed native build contracts; these run in the default core tier."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path

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


def make_dsc(tmp_path, name="git.tar.xz", contents=b"source"):
    payload = tmp_path / name
    payload.write_bytes(contents)
    digest = hashlib.sha256(contents).hexdigest()
    dsc = tmp_path / "git.dsc"
    dsc.write_text(
        f"Format: 3.0 (quilt)\nChecksums-Sha256:\n {digest} {len(contents)} {name}\nFiles:\n",
        encoding="utf-8",
    )
    return dsc, payload, digest


def test_every_dsc_source_component_is_checked(tmp_path):
    dsc, payload, digest = make_dsc(tmp_path)
    assert audit.source_components(dsc) == {payload.name: digest}
    payload.write_bytes(b"change")
    with pytest.raises(ValueError, match="checksum"):
        audit.source_components(dsc)


@pytest.mark.parametrize(
    "name", ["../git.tar.xz", "/git.tar.xz", "git/part", "-option"]
)
def test_dsc_rejects_unsafe_component_names(tmp_path, name):
    dsc, _, _ = make_dsc(tmp_path)
    dsc.write_text(
        dsc.read_text(encoding="utf-8").replace("git.tar.xz", name), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="component"):
        audit.source_components(dsc)


def test_dsc_missing_checksum_and_duplicate_component_fail(tmp_path):
    dsc, _, _ = make_dsc(tmp_path)
    text = dsc.read_text(encoding="utf-8")
    dsc.write_text(
        text.replace("Files:", text.splitlines()[2] + "\nFiles:"), encoding="utf-8"
    )
    with pytest.raises(ValueError):
        audit.source_components(dsc)
    dsc.write_text("Format: 3.0 (quilt)\n", encoding="utf-8")
    with pytest.raises(ValueError):
        audit.source_components(dsc)


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


def test_corrupt_second_source_component_also_fails(tmp_path):
    dsc, _, _ = make_dsc(tmp_path)
    second = tmp_path / "git.debian.tar.xz"
    second.write_bytes(b"patches")
    digest = audit.sha(second)
    text = dsc.read_text(encoding="utf-8").replace(
        "Files:",
        f" {digest} 7 {second.name}\nFiles:",
    )
    dsc.write_text(text, encoding="utf-8")
    assert len(audit.source_components(dsc)) == 2
    second.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        audit.source_components(dsc)


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
        "dpkg-source -x",
        "dpkg-parsechangelog -S Version",
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


def test_reconstruction_instructions_include_all_root_inputs():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    instructions = (ROOT / "scripts/minimal_git/README.md").read_text(encoding="utf-8")
    assert "COPY Dockerfile LICENSE /recipe/" in dockerfile
    assert "`Dockerfile` and `LICENSE` at its root" in instructions
    assert "`scripts/minimal_git/`" in instructions
