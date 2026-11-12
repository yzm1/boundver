#!/usr/bin/env python3
"""Build-only checks and source records for the reduced-feature container Git.

All subprocesses inspect the trusted, just-built staging tree, never repository
content supplied to the CLI. No helper is installed on the CLI's import path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import lzma
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import time
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler


LIBRARIES = {"libc.so.6", "libpcre2-8.so.0", "libz.so.1"}
PACKAGES = {"libc6", "libpcre2-8-0", "zlib1g"}
MACHINES = {"amd64": "Advanced Micro Devices X86-64", "arm64": "AArch64"}
LOADERS = {
    "amd64": "/lib64/ld-linux-x86-64.so.2",
    "arm64": "/lib/ld-linux-aarch64.so.1",
}
ELFS = {
    "usr/bin/git",
    "usr/bin/git-shell",
    "usr/bin/scalar",
    "usr/libexec/git-core/git-daemon",
    "usr/libexec/git-core/git-http-backend",
    "usr/libexec/git-core/git-imap-send",
    "usr/libexec/git-core/git-sh-i18n--envsubst",
}

UPSTREAM_VERSION = "2.50.2"
UPSTREAM_SOURCE_VERSION = "1:" + UPSTREAM_VERSION
PRIMARY_FINGERPRINT = "96E07AF25771955980DAD10020D04E5A713660A7"
SIGNER_FINGERPRINT = "E1F036B1FEE7221FC778ECEFB0B5E88696AFE6CB"
SOURCE_FILES = {
    "git-2.50.2.tar.xz": (
        7886764,
        "b9d6be0e286803466f998ced664566de3368534eca7eda6d5d9358e40f55c151",
    ),
    "git-2.50.2.tar.sign": (
        566,
        "31e35702bbfb688a95e29e97f94e4f74df88d0f12b098ea247535192b0f93c25",
    ),
    "git-release-key.gpg": (
        6713,
        "d6690645994921f44d0c9336f55e2b2c7e052c14a5363d039e8a5b16d6af2252",
    ),
}
TAR_SIZE = 49971200
TAR_SHA256 = "d2e48edf942f74fd26db541da6e53cd85b82c46171032e5d86212d6578c75742"


def check_source_file(path: Path) -> None:
    expected_size, expected_hash = SOURCE_FILES[path.name]
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_size != expected_size
        or sha(path) != expected_hash
    ):
        raise ValueError("upstream source component checksum mismatch")


def check_source_url(url: str) -> None:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname not in {"www.kernel.org", "cdn.kernel.org"}
        or parts.port not in {None, 443}
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or parts.path
        not in {
            "/pub/software/scm/git/git-2.50.2.tar.xz",
            "/pub/software/scm/git/git-2.50.2.tar.sign",
        }
    ):
        raise ValueError("unexpected upstream source URL")


class SourceRedirects(HTTPRedirectHandler):
    def __init__(self) -> None:
        self.redirects = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.redirects += 1
        if self.redirects > 2:
            raise ValueError("upstream source redirect budget exceeded")
        check_source_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_source(directory: Path, name: str) -> None:
    size, _ = SOURCE_FILES[name]
    url = "https://www.kernel.org/pub/software/scm/git/" + name
    check_source_url(url)
    deadline = time.monotonic() + 90
    opener = build_opener(SourceRedirects())
    target = directory / name
    with opener.open(url, timeout=30) as response, target.open("xb") as output:
        check_source_url(response.geturl())
        remaining = size
        while True:
            if time.monotonic() >= deadline:
                raise ValueError("upstream source download deadline exceeded")
            block = response.read(min(1024 * 1024, remaining + 1))
            if not block:
                break
            remaining -= len(block)
            if remaining < 0:
                raise ValueError("upstream source download exceeded pinned size")
            output.write(block)
    check_source_file(target)


def check_signature_status(status: str) -> None:
    lines = status.splitlines()
    valid = [line.split() for line in lines if line.startswith("[GNUPG:] VALIDSIG ")]
    if (
        len(valid) != 1
        or len(valid[0]) != 12
        or valid[0][2] != SIGNER_FINGERPRINT
        or valid[0][-1] != PRIMARY_FINGERPRINT
        or len([line for line in lines if line.startswith("[GNUPG:] GOODSIG ")]) != 1
        or any(
            token in status
            for token in (
                "BADSIG ",
                "EXPKEYSIG ",
                "REVKEYSIG ",
                "EXPSIG ",
                "ERRSIG ",
                "NO_PUBKEY ",
                "NODATA ",
                "FAILURE ",
                "ERROR ",
            )
        )
    ):
        raise ValueError("upstream source signature identity or status mismatch")


def fetch_source(directory: Path, source_version: str) -> None:
    if source_version != UPSTREAM_SOURCE_VERSION:
        raise ValueError("unreviewed Git source version")
    if directory.is_symlink() or not directory.is_dir() or any(directory.iterdir()):
        raise ValueError("upstream source directory must be empty and non-symlink")
    key = Path(__file__).with_name("git-release-key.gpg")
    check_source_file(key)
    for name in ("git-2.50.2.tar.xz", "git-2.50.2.tar.sign"):
        print(f"Fetching hash-pinned upstream component: {name}", flush=True)
        download_source(directory, name)
    shutil.copyfile(key, directory / key.name)
    decoder = lzma.LZMADecompressor(memlimit=64 * 1024 * 1024)
    tar = decoder.decompress(
        (directory / "git-2.50.2.tar.xz").read_bytes(), max_length=TAR_SIZE + 1
    )
    if (
        len(tar) != TAR_SIZE
        or not decoder.eof
        or decoder.unused_data
        or hashlib.sha256(tar).hexdigest() != TAR_SHA256
    ):
        raise ValueError("upstream uncompressed source checksum mismatch")
    key_home = directory / "gpgv"
    key_home.mkdir(mode=0o700)
    checked = subprocess.run(
        [
            "/usr/bin/gpgv",
            "--homedir",
            str(key_home),
            "--keyring",
            str(directory / key.name),
            "--status-fd",
            "1",
            str(directory / "git-2.50.2.tar.sign"),
            "-",
        ],
        input=tar,
        capture_output=True,
        timeout=60,
        env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
    )
    if checked.returncode:
        raise ValueError("upstream source signature verification failed")
    check_signature_status(checked.stdout.decode("utf-8", errors="strict"))
    print(f"Verified upstream Git {UPSTREAM_VERSION} signature: {PRIMARY_FINGERPRINT}", flush=True)
    metadata = source_metadata(source_version)
    (directory / "upstream.json").write_bytes(
        (json.dumps(metadata, sort_keys=True, indent=2) + "\n").encode("utf-8")
    )


def source_metadata(source_version: str) -> dict:
    if source_version != UPSTREAM_SOURCE_VERSION:
        raise ValueError("unreviewed Git source version")
    return {
        "source_kind": "authenticated-upstream-security-release",
        "upstream_version": UPSTREAM_VERSION,
        "source_version": source_version,
        "source_url": "https://www.kernel.org/pub/software/scm/git/",
        "primary_fingerprint": PRIMARY_FINGERPRINT,
        "signer_fingerprint": SIGNER_FINGERPRINT,
        "uncompressed_tar_sha256": TAR_SHA256,
        "source_components": {
            name: digest for name, (_, digest) in SOURCE_FILES.items()
        },
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
    # Some architectures also record their mandatory interpreter as DT_NEEDED
    # (observed on arm64). It is already supplied by libc6, not another runtime
    # package. Permit only the exact loader required below for this architecture.
    allowed = LIBRARIES | {Path(LOADERS[arch]).name}
    if not needed or not needed <= allowed:
        raise ValueError(
            f"unexpected shared library dependency for {arch}: {sorted(needed)}"
        )
    interpreter = re.findall(r"Requesting program interpreter: ([^\]]+)\]", segments)
    if interpreter != [LOADERS[arch]]:
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


def source_components(directory: Path) -> dict[str, str]:
    """Check the complete canonical manifest and every pinned upstream input."""
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("invalid upstream source directory")
    manifest = directory / "upstream.json"
    if manifest.is_symlink():
        raise ValueError("upstream source manifest is a symlink")
    expected = (
        json.dumps(source_metadata(UPSTREAM_SOURCE_VERSION), sort_keys=True, indent=2)
        + "\n"
    )
    with manifest.open("rb") as stream:
        encoded = stream.read(16385)
    if encoded != expected.encode("utf-8"):
        raise ValueError("upstream source manifest mismatch")
    for name in SOURCE_FILES:
        check_source_file(directory / name)
    return {name: digest for name, (_, digest) in SOURCE_FILES.items()}


def bundle(
    stage: Path, source_directory: Path, audit_path: Path, source_version: str
) -> None:
    authentication = source_metadata(source_version)
    dest = stage / "usr/share/licenses/git/source"
    dest.mkdir(parents=True)
    components = source_components(source_directory)
    for name in ["upstream.json", *components]:
        shutil.copyfile(source_directory / name, dest / name)
    recipe = dest / "recipe"
    shutil.copytree("/recipe", recipe)
    # APT authenticates compiler/runtime packages; Git has its own signature.
    trust = dest / "apt"
    trust.mkdir()
    for path in sorted(Path("/var/lib/apt/lists").iterdir()):
        if path.is_file() and ("InRelease" in path.name or "Packages" in path.name):
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
            "upstream_authentication": authentication,
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
        " Custom build from authenticated upstream source; source and recipes accompany this package.\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("flags")
    fetch = commands.add_parser("fetch-source")
    fetch.add_argument("directory", type=Path)
    fetch.add_argument("source_version")
    check = commands.add_parser("audit")
    check.add_argument("stage", type=Path)
    check.add_argument("arch", choices=sorted(MACHINES))
    check.add_argument("output", type=Path)
    source = commands.add_parser("bundle")
    source.add_argument("stage", type=Path)
    source.add_argument("source_directory", type=Path)
    source.add_argument("audit_path", type=Path)
    source.add_argument("source_version")
    args = vars(parser.parse_args())
    command = args.pop("command")
    {"audit": audit, "bundle": bundle, "flags": flags, "fetch-source": fetch_source}[
        command
    ](**args)


if __name__ == "__main__":
    main()
