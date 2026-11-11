#!/usr/bin/env python3
"""Build-only checks and source records for the reduced-feature container Git.

All subprocesses inspect the trusted, just-built staging tree, never repository
content supplied to the CLI. No helper is installed on the CLI's import path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess


LIBRARIES = {"libc.so.6", "libpcre2-8.so.0", "libz.so.1"}
PACKAGES = {"libc6", "libpcre2-8-0", "zlib1g"}
MACHINES = {"amd64": "Advanced Micro Devices X86-64", "arm64": "AArch64"}
ELFS = {
    "usr/bin/git",
    "usr/bin/git-shell",
    "usr/bin/scalar",
    "usr/libexec/git-core/git-daemon",
    "usr/libexec/git-core/git-http-backend",
    "usr/libexec/git-core/git-imap-send",
    "usr/libexec/git-core/git-sh-i18n--envsubst",
}


def run(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        args,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=60,
        env={**os.environ, "LC_ALL": "C"},
    ).stdout


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def check_elf(
    header: str, segments: str, dynamic: str, sections: str, arch: str
) -> None:
    """Check actual ELF properties, not just requested compiler flags."""
    if arch not in MACHINES:
        raise ValueError("unsupported architecture")
    fields = dict(
        re.findall(r"^\s*(Class|Data|Type|Machine):\s*(.*?)\s*$", header, re.M)
    )
    if (
        fields.get("Class") != "ELF64"
        or fields.get("Data") != "2's complement, little endian"
        or not fields.get("Type", "").startswith("DYN ")
        or fields.get("Machine") != MACHINES[arch]
    ):
        raise ValueError("expected native ELF64 little-endian PIE")
    stacks = re.findall(r"^\s*GNU_STACK\s+.*$", segments, re.M)
    if len(stacks) != 1 or re.search(r"\bRWE\b|\bE\b", stacks[0]):
        raise ValueError("missing or executable GNU_STACK")
    if not re.search(r"^\s*GNU_RELRO\s", segments, re.M):
        raise ValueError("missing RELRO")
    if not re.search(r"\(FLAGS(?:_1)?\).*\b(?:BIND_NOW|NOW)\b", dynamic):
        raise ValueError("missing immediate binding")
    if not re.search(r"\(FLAGS_1\).*\bPIE\b", dynamic):
        raise ValueError("missing PIE flag")
    if re.search(r"\bTEXTREL\b|\((?:RPATH|RUNPATH)\)", dynamic):
        raise ValueError("text relocation or custom library search path")
    if ".debug_" in sections or ".zdebug_" in sections:
        raise ValueError("unstripped debug sections")
    needed = set(re.findall(r"\(NEEDED\).*\[([^\]]+)\]", dynamic))
    if not needed or not needed <= LIBRARIES:
        raise ValueError(
            f"unexpected shared library dependency for {arch}: {sorted(needed)}"
        )
    interpreter = re.findall(r"Requesting program interpreter: ([^\]]+)\]", segments)
    expected = {
        "amd64": "/lib64/ld-linux-x86-64.so.2",
        "arm64": "/lib/ld-linux-aarch64.so.1",
    }
    if interpreter != [expected[arch]]:
        raise ValueError("unexpected ELF interpreter")


def dependencies(output: str) -> str:
    if not output.startswith("shlibs:Depends=") or "\n" in output.strip():
        raise ValueError("unexpected dpkg-shlibdeps output")
    value = output.strip().removeprefix("shlibs:Depends=")
    parts = value.split(", ")
    names = []
    for part in parts:
        match = re.fullmatch(r"([a-z0-9+.-]+)(?: \(>= [0-9A-Za-z.+:~\-]+\))?", part)
        if not match:
            raise ValueError("unsupported dependency expression")
        names.append(match[1])
    if set(names) != PACKAGES or len(names) != len(PACKAGES):
        raise ValueError(f"unexpected package dependency set: {sorted(names)}")
    return "dash, " + value


def check_flags(cflags: str, cppflags: str, ldflags: str) -> None:
    required = (
        ({"-O2", "-fstack-protector-strong", "-fPIE"}, cflags),
        ({"-D_FORTIFY_SOURCE=3"}, cppflags),
        ({"-pie", "-Wl,-z,relro,-z,now"}, ldflags),
    )
    for expected, actual in required:
        missing = expected - set(shlex.split(actual))
        if missing:
            raise ValueError(
                f"required compiler hardening flags absent: {sorted(missing)}"
            )


def flags() -> None:
    values = {key: os.environ[key] for key in ("CFLAGS", "CPPFLAGS", "LDFLAGS")}
    check_flags(*values.values())
    print(json.dumps(values, sort_keys=True))


def audit(stage: Path, arch: str, output: Path) -> None:
    stage = stage.resolve(strict=True)
    records = []
    scripts = []
    seen = set()
    for path in sorted(stage.rglob("*")):
        if path.is_symlink():
            if not path.resolve(strict=True).is_relative_to(stage):
                raise ValueError("staged symlink escapes package")
            continue
        if not path.is_file():
            continue
        with path.open("rb") as stream:
            prefix = stream.read(256)
        relative = path.relative_to(stage).as_posix()
        if prefix.startswith(b"#!"):
            # Linux does not normalize CRLF in a shebang. Inspect raw bytes,
            # and require a complete, LF-terminated known interpreter line.
            if (
                prefix.partition(b"\n")[0] not in (b"#!/bin/sh", b"#! /bin/sh")
                or b"\n" not in prefix
            ):
                raise ValueError(f"unexpected script interpreter: {relative}")
            scripts.append(relative)
        if not prefix.startswith(b"\x7fELF"):
            if path.stat().st_mode & 0o111 and not prefix.startswith(b"#!"):
                raise ValueError(f"unrecognized executable format: {relative}")
            continue
        inode = (path.stat().st_dev, path.stat().st_ino)
        if inode in seen:
            continue
        seen.add(inode)
        check_elf(
            run("readelf", "-hW", str(path)),
            run("readelf", "-lW", str(path)),
            run("readelf", "-dW", str(path)),
            run("readelf", "-SW", str(path)),
            arch,
        )
        records.append({"path": relative, "sha256": sha(path)})
    if {entry["path"] for entry in records} != ELFS:
        raise ValueError(f"unexpected executable inventory: {records}")
    # Charge every unique installed ELF, not just /usr/bin/git.
    dep = dependencies(
        run(
            "dpkg-shlibdeps",
            "-O",
            *("-e" + str(stage / entry["path"]) for entry in records),
        )
    )
    output.write_text(
        json.dumps(
            {
                "architecture": arch,
                "depends": dep,
                "elfs": records,
                "shell_scripts": scripts,
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def source_components(dsc: Path) -> dict[str, str]:
    """Check all source components named by the authenticated .dsc."""
    text = dsc.read_text(encoding="utf-8")
    section = re.search(r"(?m)^Checksums-Sha256:\n((?: [^\n]+\n)+)", text)
    if not section:
        raise ValueError("missing source SHA256 section")
    records = {}
    for line in section[1].splitlines():
        match = re.fullmatch(
            r" ([0-9a-f]{64}) ([0-9]+) ([A-Za-z0-9][A-Za-z0-9.+~_-]*)", line
        )
        if not match or match[3] in records:
            raise ValueError("invalid source component")
        path = dsc.parent / match[3]
        if (
            path.is_symlink()
            or path.stat().st_size != int(match[2])
            or sha(path) != match[1]
        ):
            raise ValueError("source component checksum mismatch")
        records[match[3]] = match[1]
    if not records:
        raise ValueError("empty source component list")
    return records


def bundle(stage: Path, dsc: Path, audit_path: Path, source_version: str) -> None:
    if not re.fullmatch(r"[0-9]+:[0-9][0-9A-Za-z.+~\-]*", source_version):
        raise ValueError("invalid source version")
    dest = stage / "usr/share/licenses/git/source"
    dest.mkdir(parents=True)
    components = source_components(dsc)
    for name in [dsc.name, *components]:
        shutil.copyfile(dsc.parent / name, dest / name)
    recipe = dest / "recipe"
    shutil.copytree("/recipe", recipe)
    # Retain the source index/signatures/keyring used by authenticated APT.
    trust = dest / "apt"
    trust.mkdir()
    for path in sorted(Path("/var/lib/apt/lists").iterdir()):
        if path.is_file() and ("InRelease" in path.name or "Sources" in path.name):
            shutil.copyfile(path, trust / path.name)
    shutil.copyfile("/etc/apt/sources.list.d/debian.sources", trust / "debian.sources")
    shutil.copyfile(
        "/usr/share/keyrings/debian-archive-keyring.gpg",
        trust / "debian-archive-keyring.gpg",
    )
    metadata = json.loads(audit_path.read_text(encoding="utf-8"))
    check_flags(*(os.environ[key] for key in ("CFLAGS", "CPPFLAGS", "LDFLAGS")))
    metadata.update(
        {
            "source_version": source_version,
            "binary_version": source_version + "+boundver1",
            "source_components": components,
            "builder_packages": run("dpkg-query", "-W", "-f=${Package}\t${Version}\n"),
            # Prefix maps differ deliberately between the independent build dirs.
            "flags": {
                key: os.environ[key].replace(str(Path.cwd()), "<build>")
                for key in ("CFLAGS", "CPPFLAGS", "LDFLAGS")
            },
            "sha1_collision_test": "SHA1DC prerequisite and shattered-1.pdf rejection passed",
        }
    )
    (dest / "build.json").write_text(
        json.dumps(metadata, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        f"{sha(p)}  {p.relative_to(dest).as_posix()}\n"
        for p in sorted(dest.rglob("*"))
        if p.is_file()
    ]
    (dest / "SHA256SUMS").write_text("".join(lines), encoding="utf-8")
    control = stage / "DEBIAN"
    control.mkdir()
    (control / "control").write_text(
        "Package: git\n"
        f"Version: {metadata['binary_version']}\n"
        f"Source: git ({source_version})\n"
        f"Architecture: {metadata['architecture']}\n"
        "Maintainer: Boundver maintainers <noreply@boundver.invalid>\n"
        f"Depends: {metadata['depends']}\n"
        "Description: Reduced-feature Git for local Boundver inspection\n"
        " Custom build from Debian source; source and recipes accompany this package.\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("flags")
    check = commands.add_parser("audit")
    check.add_argument("stage", type=Path)
    check.add_argument("arch", choices=sorted(MACHINES))
    check.add_argument("output", type=Path)
    source = commands.add_parser("bundle")
    source.add_argument("stage", type=Path)
    source.add_argument("dsc", type=Path)
    source.add_argument("audit_path", type=Path)
    source.add_argument("source_version")
    args = vars(parser.parse_args())
    command = args.pop("command")
    {"audit": audit, "bundle": bundle, "flags": flags}[command](**args)


if __name__ == "__main__":
    main()
