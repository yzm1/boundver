#!/usr/bin/env bash
set -euo pipefail

# This script intentionally removes generated build directories below. Resolve
# its own repository root before the first helper invocation or deletion, and
# fail closed if a caller launched it from anywhere else.
invocation_root=$PWD
script_source=${BASH_SOURCE[0]}
case "$script_source" in
  /*) ;;
  *) script_source="$invocation_root/$script_source" ;;
esac
if [[ -L "$script_source" || ! -f "$script_source" ]]; then
  echo "Packaging smoke script must be invoked from its regular repository file." >&2
  exit 2
fi
if ! cd -P -- "$invocation_root"; then
  echo "Unable to resolve the packaging smoke working directory." >&2
  exit 2
fi
invocation_root=$PWD
if ! cd -P -- "${script_source%/*}/.."; then
  echo "Unable to resolve the packaging smoke repository root." >&2
  exit 2
fi
repository_root=$PWD
if [[ "$invocation_root" != "$repository_root" ]]; then
  echo "Run scripts/packaging_smoke.sh from the repository root." >&2
  exit 2
fi

# The release build runs on Python 3.12. Install its complete reviewed tool
# closure with local SHA-256 hashes before disabling build isolation below.
python -I scripts/install_locked_tools.py release
# Generated backend state must not shadow the `build` frontend or leak stale
# package metadata into a repeat smoke run.
python -I scripts/build_release_artifacts.py --clean
python -I scripts/build_release_artifacts.py --output-dir dist

set -- dist/*.whl
if [[ $# -ne 1 || ! -f "$1" ]]; then
  echo "Expected exactly one wheel in dist/" >&2
  exit 1
fi
wheel_path="$PWD/$1"

set -- dist/*.tar.gz
if [[ $# -ne 1 || ! -f "$1" ]]; then
  echo "Expected exactly one source distribution in dist/" >&2
  exit 1
fi
sdist_path="$PWD/$1"

expected_version=$(python -I - <<'PY' | head -c 129
import os
import re
import stat
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib

MAX_PROJECT_BYTES = 1024 * 1024
MAX_VERSION_BYTES = 128
MAX_TOML_INTEGER_DIGITS = 640
READ_CHUNK_BYTES = 64 * 1024


def is_reparse_point(identity):
    attributes = getattr(identity, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def changed(before, after):
    return (
        (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        or before.st_mode != after.st_mode
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    )


def toml_has_oversized_numeric_token(text):
    """Detect oversized TOML numeric values outside strings and comments."""
    index = 0
    length = len(text)
    state = "normal"
    root_in_value = False
    table_header_depth = 0
    at_statement_start = True
    array_frame = 0
    inline_key_frame = 1
    inline_value_frame = 2
    containers = bytearray()

    def in_key_context():
        if table_header_depth:
            return True
        if containers:
            return containers[-1] == inline_key_frame
        return not root_in_value

    def reset_line():
        nonlocal root_in_value, table_header_depth, at_statement_start
        if not containers:
            root_in_value = False
            table_header_depth = 0
            at_statement_start = True

    while index < length:
        char = text[index]
        if state == "comment":
            if char in "\r\n":
                state = "normal"
                reset_line()
            index += 1
            continue
        if state in {"basic", "multiline-basic"}:
            if char == "\\":
                index += 2
                continue
            if state == "basic" and char == '"':
                state = "normal"
                index += 1
                continue
            if state == "multiline-basic" and char == '"':
                end = index
                while end < length and text[end] == '"':
                    end += 1
                if end - index >= 3:
                    state = "normal"
                index = end
                continue
            index += 1
            continue
        if state in {"literal", "multiline-literal"}:
            if state == "literal" and char == "'":
                state = "normal"
                index += 1
                continue
            if state == "multiline-literal" and char == "'":
                end = index
                while end < length and text[end] == "'":
                    end += 1
                if end - index >= 3:
                    state = "normal"
                index = end
                continue
            index += 1
            continue
        if char == "#":
            state = "comment"
            index += 1
            continue
        if char in "\r\n":
            reset_line()
            index += 1
            continue
        if char in " \t":
            index += 1
            continue
        if text.startswith('\"\"\"', index):
            state = "multiline-basic"
            at_statement_start = False
            index += 3
            continue
        if text.startswith("'''", index):
            state = "multiline-literal"
            at_statement_start = False
            index += 3
            continue
        if char == '"':
            state = "basic"
            at_statement_start = False
            index += 1
            continue
        if char == "'":
            state = "literal"
            at_statement_start = False
            index += 1
            continue
        if char == "[":
            if table_header_depth:
                table_header_depth += 1
            elif not containers and not root_in_value and at_statement_start:
                table_header_depth = 1
            else:
                containers.append(array_frame)
            at_statement_start = False
            index += 1
            continue
        if char == "]":
            if table_header_depth:
                table_header_depth -= 1
            elif containers and containers[-1] == array_frame:
                containers.pop()
            at_statement_start = False
            index += 1
            continue
        if char == "{":
            containers.append(inline_key_frame)
            at_statement_start = False
            index += 1
            continue
        if char == "}":
            if containers and containers[-1] in {
                inline_key_frame,
                inline_value_frame,
            }:
                containers.pop()
            at_statement_start = False
            index += 1
            continue
        if char == "=":
            if containers and containers[-1] == inline_key_frame:
                containers[-1] = inline_value_frame
            elif not containers and not table_header_depth:
                root_in_value = True
            at_statement_start = False
            index += 1
            continue
        if char == ",":
            if containers and containers[-1] == inline_value_frame:
                containers[-1] = inline_key_frame
            at_statement_start = False
            index += 1
            continue
        if (
            not in_key_context()
            and char == "0"
            and index + 1 < length
            and text[index + 1] in "bBoOxX"
        ):
            prefix = text[index + 1].lower()
            valid_digits = {
                "b": "01",
                "o": "01234567",
                "x": "0123456789abcdefABCDEF",
            }[prefix]
            index += 2
            digits = 0
            while index < length and (
                text[index] in valid_digits or text[index] == "_"
            ):
                if text[index] != "_":
                    digits += 1
                    if digits > MAX_TOML_INTEGER_DIGITS:
                        return True
                index += 1
            continue
        if not in_key_context() and "0" <= char <= "9":
            digits = 0
            while index < length and (
                "0" <= text[index] <= "9" or text[index] == "_"
            ):
                if text[index] != "_":
                    digits += 1
                    if digits > MAX_TOML_INTEGER_DIGITS:
                        return True
                index += 1
            continue
        at_statement_start = False
        index += 1
    return False


def plain_directory(path, label):
    identity = path.lstat()
    if not stat.S_ISDIR(identity.st_mode) or is_reparse_point(identity):
        raise ValueError(f"{label} is not a plain directory")
    return identity


def read_project(path):
    parent = path.parent
    parent_initial = plain_directory(parent, "project metadata directory")
    initial = path.lstat()
    if not stat.S_ISREG(initial.st_mode) or is_reparse_point(initial):
        raise ValueError("pyproject.toml is not a regular file")
    if initial.st_size > MAX_PROJECT_BYTES:
        raise ValueError(
            f"pyproject.toml exceeds the {MAX_PROJECT_BYTES}-byte limit"
        )
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or changed(initial, opened):
            raise ValueError("pyproject.toml changed while opening")
        content = bytearray()
        while True:
            remaining = MAX_PROJECT_BYTES - len(content)
            chunk = stream.read(min(READ_CHUNK_BYTES, remaining + 1))
            if not chunk:
                break
            if len(chunk) > remaining:
                raise ValueError(
                    f"pyproject.toml exceeds the {MAX_PROJECT_BYTES}-byte limit"
                )
            content.extend(chunk)
        finished = os.fstat(stream.fileno())
    current = path.lstat()
    parent_current = plain_directory(parent, "project metadata directory")
    if (
        not stat.S_ISREG(current.st_mode)
        or is_reparse_point(current)
        or changed(opened, finished)
        or changed(finished, current)
        or finished.st_size != len(content)
        or changed(parent_initial, parent_current)
    ):
        raise ValueError("pyproject.toml changed while reading")
    return bytes(content).decode("utf-8")


try:
    project_text = read_project(Path("pyproject.toml"))
    if toml_has_oversized_numeric_token(project_text):
        raise ValueError(
            "pyproject.toml contains a numeric token exceeding the "
            f"{MAX_TOML_INTEGER_DIGITS}-decimal-digit limit"
        )
    project = tomllib.loads(project_text)["project"]
    version = project["version"]
    if (
        not isinstance(version, str)
        or len(version.encode("utf-8")) > MAX_VERSION_BYTES
        or re.fullmatch(
            r"[A-Za-z0-9](?:[A-Za-z0-9._+!-]{0,126}[A-Za-z0-9])?",
            version,
        )
        is None
        or ".." in version
    ):
        raise ValueError("project.version is unsafe or exceeds its output limit")
except (KeyError, OSError, TypeError, UnicodeError, ValueError) as error:
    raise SystemExit(f"Cannot read bounded project.version: {error}") from error
print(version)
PY
)
if [[ -z "$expected_version" || ${#expected_version} -gt 128 ]]; then
  echo "Expected one bounded project.version value" >&2
  exit 1
fi

# Keep the captured path plus one growth sentinel bounded. `pipefail` also
# preserves a failing `mktemp` status through the limiter.
smoke_root=$(
  mktemp -d "${TMPDIR:-/tmp}/bv-pkg.XXXXXXXXXX" |
    head -c 4097
)
smoke_leaf=${smoke_root##*/}
if [[
  -z "$smoke_root" ||
  ${#smoke_root} -gt 4096 ||
  "$smoke_root" == *$'\n'* ||
  "$smoke_leaf" != bv-pkg.* ||
  ! -d "$smoke_root" ||
  -L "$smoke_root"
]]; then
  echo "mktemp returned an unsafe temporary directory path" >&2
  exit 1
fi
unset smoke_leaf
trap 'rm -rf "$smoke_root"' EXIT
pyz_path="$PWD/dist/boundver-$expected_version.pyz"
if [[ ! -f "$pyz_path" ]]; then
  echo "Expected reproducible standalone archive at $pyz_path" >&2
  exit 1
fi

python -I - "$wheel_path" "$sdist_path" "$pyz_path" "$expected_version" "$PWD/LICENSE" "$smoke_root" "$PWD/scripts/requirements/action.lock" <<'PY'
from contextlib import contextmanager
import email.parser
import gzip
import os
import re
import stat
import struct
import sys
import tarfile
import unicodedata
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile


MAX_ARCHIVE_SOURCE_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_SOURCE_TOTAL_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10_000
MAX_ARCHIVE_HEADERS = (2 * MAX_ARCHIVE_MEMBERS) + 32
MAX_ARCHIVE_NAME_BYTES = 4 * 1024
MAX_ARCHIVE_TOTAL_NAME_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_MEMBER_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 512 * 1024 * 1024
MAX_ZIP_CENTRAL_BYTES = 16 * 1024 * 1024
MAX_TAR_EXTENSION_BYTES = 64 * 1024
MAX_TAR_BYTES = 768 * 1024 * 1024
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_LICENSE_BYTES = 4 * 1024 * 1024
MAX_TOOL_LOCK_BYTES = 4 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
WINDOWS_RESERVED_ARCHIVE_STEMS = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


def is_reparse_point(identity):
    attributes = getattr(identity, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def changed(before, after):
    return (
        (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        or before.st_mode != after.st_mode
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    )


def plain_directory(path, label):
    identity = path.lstat()
    if not stat.S_ISDIR(identity.st_mode) or is_reparse_point(identity):
        raise ValueError(f"{label} is not a plain directory")
    return identity


@contextmanager
def stable_file(path, max_bytes, label):
    parent = path.parent
    parent_initial = plain_directory(parent, f"{label} containing directory")
    initial = path.lstat()
    if not stat.S_ISREG(initial.st_mode) or is_reparse_point(initial):
        raise ValueError(f"{label} is not a regular file")
    if initial.st_size > max_bytes:
        raise ValueError(f"{label} exceeds the {max_bytes}-byte limit")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or changed(initial, opened):
            raise ValueError(f"{label} changed while opening")
        try:
            yield stream, opened.st_size
        finally:
            finished = os.fstat(stream.fileno())
            try:
                current = path.lstat()
                parent_current = plain_directory(
                    parent, f"{label} containing directory"
                )
            except FileNotFoundError as error:
                raise ValueError(f"{label} disappeared while reading") from error
            if (
                not stat.S_ISREG(current.st_mode)
                or is_reparse_point(current)
                or changed(opened, finished)
                or changed(finished, current)
                or changed(parent_initial, parent_current)
            ):
                raise ValueError(f"{label} changed while reading")


def read_limited_stream(stream, max_bytes, label, expected_size=None):
    content = bytearray()
    while True:
        remaining = max_bytes - len(content)
        chunk = stream.read(min(READ_CHUNK_BYTES, remaining + 1))
        if not chunk:
            break
        if len(chunk) > remaining:
            raise ValueError(f"{label} exceeds the {max_bytes}-byte limit")
        content.extend(chunk)
    if expected_size is not None and len(content) != expected_size:
        raise ValueError(f"{label} changed size while being read")
    return bytes(content)


def read_limited_file(path, max_bytes, label):
    with stable_file(path, max_bytes, label) as (stream, expected_size):
        return read_limited_stream(stream, max_bytes, label, expected_size)


def preflight_source_total(sources):
    total = 0
    for path, label in sources:
        identity = path.lstat()
        if not stat.S_ISREG(identity.st_mode) or is_reparse_point(identity):
            raise ValueError(f"{label} is not a regular file")
        if identity.st_size > MAX_ARCHIVE_SOURCE_BYTES:
            raise ValueError(
                f"{label} exceeds the {MAX_ARCHIVE_SOURCE_BYTES}-byte limit"
            )
        total += identity.st_size
        if total > MAX_ARCHIVE_SOURCE_TOTAL_BYTES:
            raise ValueError(
                "release archives exceed the "
                f"{MAX_ARCHIVE_SOURCE_TOTAL_BYTES}-byte aggregate source limit"
            )


def read_exact(stream, size, label):
    content = bytearray()
    while len(content) < size:
        chunk = stream.read(size - len(content))
        if not chunk:
            raise ValueError(f"{label} is truncated")
        content.extend(chunk)
    return bytes(content)


def validate_archive_name(name, label):
    try:
        encoded_length = len(name.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise ValueError(f"{label} contains a non-UTF-8 member name") from error
    stripped = name[:-1] if name.endswith("/") else name
    parts = stripped.split("/")
    if (
        not stripped
        or encoded_length > MAX_ARCHIVE_NAME_BYTES
        or PurePosixPath(name).is_absolute()
        or "\\" in name
        or any(part in {"", ".", ".."} for part in parts)
        or any(":" in part for part in parts)
        or any(part.endswith((" ", ".")) for part in parts)
        or any(
            part.split(".", 1)[0].casefold() in WINDOWS_RESERVED_ARCHIVE_STEMS
            for part in parts
        )
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        raise ValueError(f"{label} contains an unsafe or overlong member name")
    return encoded_length, unicodedata.normalize("NFC", stripped).casefold()


def preflight_zip(stream, source_size, label):
    tail_size = min(source_size, 65_557)
    stream.seek(source_size - tail_size)
    tail = read_exact(stream, tail_size, f"{label} ZIP trailer")
    eocd_index = tail.rfind(b"PK\x05\x06")
    if eocd_index < 0 or len(tail) - eocd_index < 22:
        raise ValueError(f"{label} has no complete ZIP directory trailer")
    (
        signature,
        disk_number,
        directory_disk,
        disk_entries,
        total_entries,
        directory_size,
        directory_offset,
        comment_size,
    ) = struct.unpack_from("<4s4H2LH", tail, eocd_index)
    if signature != b"PK\x05\x06":
        raise ValueError(f"{label} has an invalid ZIP directory trailer")
    if eocd_index + 22 + comment_size != len(tail):
        raise ValueError(f"{label} has malformed ZIP trailing data")
    if (
        disk_number != 0
        or directory_disk != 0
        or disk_entries != total_entries
        or total_entries == 0xFFFF
        or directory_size == 0xFFFFFFFF
        or directory_offset == 0xFFFFFFFF
    ):
        raise ValueError(f"{label} uses unsupported split or ZIP64 metadata")
    if total_entries > MAX_ARCHIVE_MEMBERS:
        raise ValueError(
            f"{label} exceeds the {MAX_ARCHIVE_MEMBERS}-member limit"
        )
    if directory_size > MAX_ZIP_CENTRAL_BYTES:
        raise ValueError(
            f"{label} exceeds the {MAX_ZIP_CENTRAL_BYTES}-byte ZIP directory limit"
        )
    eocd_offset = source_size - tail_size + eocd_index
    if eocd_offset >= 20:
        stream.seek(eocd_offset - 20)
        if read_exact(stream, 4, f"{label} ZIP64 locator") == b"PK\x06\x07":
            raise ValueError(f"{label} uses unsupported ZIP64 metadata")
    prefix_size = eocd_offset - directory_size - directory_offset
    directory_start = directory_offset + prefix_size
    if (
        prefix_size < 0
        or directory_start < 0
        or directory_start + directory_size != eocd_offset
    ):
        raise ValueError(f"{label} has invalid ZIP directory bounds")

    stream.seek(directory_start)
    count = 0
    total_names = 0
    total_members = 0
    names = set()
    portable_names = set()
    while stream.tell() < eocd_offset:
        header = read_exact(stream, 46, f"{label} ZIP directory")
        values = struct.unpack("<4s6H3L5H2L", header)
        if values[0] != b"PK\x01\x02":
            raise ValueError(f"{label} has an invalid ZIP directory entry")
        count += 1
        if count > MAX_ARCHIVE_MEMBERS:
            raise ValueError(
                f"{label} exceeds the {MAX_ARCHIVE_MEMBERS}-member limit"
            )
        flags = values[3]
        compressed_size = values[8]
        uncompressed_size = values[9]
        name_size = values[10]
        extra_size = values[11]
        member_comment_size = values[12]
        member_disk = values[13]
        local_offset = values[16]
        if (
            compressed_size == 0xFFFFFFFF
            or uncompressed_size == 0xFFFFFFFF
            or local_offset == 0xFFFFFFFF
            or member_disk != 0
        ):
            raise ValueError(f"{label} uses unsupported ZIP64 member metadata")
        if name_size == 0 or name_size > MAX_ARCHIVE_NAME_BYTES:
            raise ValueError(f"{label} contains an overlong ZIP member name")
        name_bytes = read_exact(stream, name_size, f"{label} ZIP member name")
        try:
            encoding = "utf-8" if flags & 0x800 else "cp437"
            name = name_bytes.decode(encoding)
        except UnicodeError as error:
            raise ValueError(f"{label} contains an invalid ZIP member name") from error
        encoded_length, portable_name = validate_archive_name(name, label)
        total_names += encoded_length
        if name in names:
            raise ValueError(f"{label} contains duplicate member names")
        if portable_name in portable_names:
            raise ValueError(f"{label} contains a non-portable member-name collision")
        names.add(name)
        portable_names.add(portable_name)
        if total_names > MAX_ARCHIVE_TOTAL_NAME_BYTES:
            raise ValueError(
                f"{label} member names exceed the "
                f"{MAX_ARCHIVE_TOTAL_NAME_BYTES}-byte aggregate limit"
            )
        if uncompressed_size > MAX_ARCHIVE_MEMBER_BYTES:
            raise ValueError(
                f"{label} contains a member exceeding the "
                f"{MAX_ARCHIVE_MEMBER_BYTES}-byte limit"
            )
        total_members += uncompressed_size
        if total_members > MAX_ARCHIVE_TOTAL_BYTES:
            raise ValueError(
                f"{label} exceeds the {MAX_ARCHIVE_TOTAL_BYTES}-byte "
                "uncompressed aggregate limit"
            )
        next_offset = stream.tell() + extra_size + member_comment_size
        if next_offset > eocd_offset:
            raise ValueError(f"{label} has a truncated ZIP directory entry")
        stream.seek(next_offset)
    if stream.tell() != eocd_offset or count != total_entries:
        raise ValueError(f"{label} has inconsistent ZIP member metadata")
    stream.seek(0)


def bounded_zip_inventory(archive, label):
    names = set()
    portable_names = set()
    total_names = 0
    total_members = 0
    infos = archive.infolist()
    if len(infos) > MAX_ARCHIVE_MEMBERS:
        raise ValueError(f"{label} exceeds the archive member limit")
    for info in infos:
        encoded_length, portable_name = validate_archive_name(info.filename, label)
        total_names += encoded_length
        if total_names > MAX_ARCHIVE_TOTAL_NAME_BYTES:
            raise ValueError(f"{label} exceeds the member-name aggregate limit")
        if info.filename in names:
            raise ValueError(f"{label} contains duplicate member names")
        if portable_name in portable_names:
            raise ValueError(f"{label} contains a non-portable member-name collision")
        names.add(info.filename)
        portable_names.add(portable_name)
        if info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
            raise ValueError(f"{label} contains an oversized archive member")
        total_members += info.file_size
        if total_members > MAX_ARCHIVE_TOTAL_BYTES:
            raise ValueError(f"{label} exceeds the member-byte aggregate limit")
    return names, infos


def read_zip_member(archive, info, max_bytes, label):
    if info.file_size > max_bytes:
        raise ValueError(f"{label} exceeds the {max_bytes}-byte limit")
    with archive.open(info, "r") as stream:
        return read_limited_stream(stream, max_bytes, label, info.file_size)


def tar_size(field, label):
    if field and field[0] & 0x80:
        value = int.from_bytes(bytes([field[0] & 0x7F]) + field[1:], "big")
    else:
        encoded = field.rstrip(b"\0 ").lstrip(b" ")
        if not encoded:
            return 0
        if any(byte not in b"01234567" for byte in encoded):
            raise ValueError(f"{label} contains an invalid TAR size")
        value = int(encoded, 8)
    if value < 0:
        raise ValueError(f"{label} contains a negative TAR size")
    return value


def preflight_tar(stream, source_size, label):
    header_count = 0
    member_count = 0
    total_members = 0
    stream.seek(0)
    while stream.tell() < source_size:
        header = read_exact(stream, 512, f"{label} TAR header")
        if header == bytes(512):
            break
        header_count += 1
        if header_count > MAX_ARCHIVE_HEADERS:
            raise ValueError(
                f"{label} exceeds the {MAX_ARCHIVE_HEADERS}-header limit"
            )
        size = tar_size(header[124:136], label)
        member_type = header[156:157]
        if member_type in {b"x", b"g", b"L", b"K"}:
            if size > MAX_TAR_EXTENSION_BYTES:
                raise ValueError(f"{label} has oversized TAR extension metadata")
        else:
            member_count += 1
            if member_count > MAX_ARCHIVE_MEMBERS:
                raise ValueError(
                    f"{label} exceeds the {MAX_ARCHIVE_MEMBERS}-member limit"
                )
            if size > MAX_ARCHIVE_MEMBER_BYTES:
                raise ValueError(f"{label} contains an oversized TAR member")
            total_members += size
            if total_members > MAX_ARCHIVE_TOTAL_BYTES:
                raise ValueError(f"{label} exceeds the TAR byte aggregate limit")
        padded_size = ((size + 511) // 512) * 512
        if stream.tell() + padded_size > source_size:
            raise ValueError(f"{label} is truncated")
        stream.seek(padded_size, 1)


def decompress_sdist(stream, destination, label):
    total = 0
    with gzip.GzipFile(fileobj=stream, mode="rb") as compressed, destination.open(
        "xb"
    ) as target:
        while True:
            remaining = MAX_TAR_BYTES - total
            chunk = compressed.read(min(READ_CHUNK_BYTES, remaining + 1))
            if not chunk:
                break
            if len(chunk) > remaining:
                raise ValueError(
                    f"{label} exceeds the {MAX_TAR_BYTES}-byte expanded limit"
                )
            written = target.write(chunk)
            if written != len(chunk):
                raise OSError(f"short write while expanding {label}")
            total += len(chunk)


def bounded_tar_inventory(stream, label):
    names = set()
    portable_names = set()
    total_names = 0
    total_members = 0
    count = 0
    stream.seek(0)
    with tarfile.open(fileobj=stream, mode="r:") as archive:
        for member in archive:
            count += 1
            if count > MAX_ARCHIVE_MEMBERS:
                raise ValueError(f"{label} exceeds the archive member limit")
            encoded_length, portable_name = validate_archive_name(member.name, label)
            total_names += encoded_length
            if total_names > MAX_ARCHIVE_TOTAL_NAME_BYTES:
                raise ValueError(f"{label} exceeds the member-name aggregate limit")
            if member.name in names:
                raise ValueError(f"{label} contains duplicate member names")
            if portable_name in portable_names:
                raise ValueError(
                    f"{label} contains a non-portable member-name collision"
                )
            names.add(member.name)
            portable_names.add(portable_name)
            if not (member.isfile() or member.isdir()):
                raise ValueError(f"{label} contains a non-regular archive member")
            if member.size > MAX_ARCHIVE_MEMBER_BYTES:
                raise ValueError(f"{label} contains an oversized archive member")
            if member.isfile():
                total_members += member.size
                if total_members > MAX_ARCHIVE_TOTAL_BYTES:
                    raise ValueError(f"{label} exceeds the member-byte aggregate limit")
    return names

wheel = Path(sys.argv[1])
sdist = Path(sys.argv[2])
pyz = Path(sys.argv[3])
expected_version = sys.argv[4]
license_path = Path(sys.argv[5])
scratch = Path(sys.argv[6])
action_lock = Path(sys.argv[7])
raw_tar = scratch / "packaging-smoke-sdist.tar"

try:
    expected_license_path = Path.cwd() / "LICENSE"
    if os.path.normcase(os.path.abspath(license_path)) != os.path.normcase(
        os.path.abspath(expected_license_path)
    ):
        raise ValueError("repository LICENSE path escapes the working tree root")
    license_path = Path("LICENSE")
    scratch_identity = scratch.lstat()
    if (
        not stat.S_ISDIR(scratch_identity.st_mode)
        or is_reparse_point(scratch_identity)
    ):
        raise ValueError("packaging smoke temporary root is unsafe")
    license_bytes = read_limited_file(
        license_path, MAX_LICENSE_BYTES, "repository LICENSE"
    )
    lock_text = read_limited_file(
        action_lock, MAX_TOOL_LOCK_BYTES, "Action dependency lock"
    ).decode("utf-8")
    pyyaml_versions = re.findall(
        r"(?mi)^PyYAML==([A-Za-z0-9][A-Za-z0-9._+!-]{0,126})\s*\\\s*$",
        lock_text,
    )
    if len(pyyaml_versions) != 1:
        raise ValueError("Action dependency lock has no unique exact PyYAML pin")
    pyyaml_version = pyyaml_versions[0]
    preflight_source_total(
        (
            (wheel, "wheel"),
            (sdist, "source distribution"),
            (pyz, "standalone archive"),
        )
    )

    with stable_file(wheel, MAX_ARCHIVE_SOURCE_BYTES, "wheel") as (
        wheel_stream,
        wheel_size,
    ):
        preflight_zip(wheel_stream, wheel_size, "wheel")
        with ZipFile(wheel_stream) as archive:
            wheel_members, wheel_infos = bounded_zip_inventory(archive, "wheel")
            metadata_infos = [
                info
                for info in wheel_infos
                if info.filename.endswith(".dist-info/METADATA")
            ]
            if len(metadata_infos) != 1:
                raise ValueError("wheel must contain exactly one METADATA member")
            wheel_metadata = email.parser.BytesParser().parsebytes(
                read_zip_member(
                    archive,
                    metadata_infos[0],
                    MAX_METADATA_BYTES,
                    "wheel METADATA",
                )
            )
            required_wheel = {
                "boundver/__main__.py",
                "boundver/boundary.config.schema.json",
                "boundver/py.typed",
            }
            missing_wheel = required_wheel - wheel_members
            if missing_wheel:
                raise ValueError(f"wheel missing: {sorted(missing_wheel)}")
            if wheel_metadata["Version"] != expected_version:
                raise ValueError("wheel version does not match project.version")
            if not any(
                name.endswith(".dist-info/licenses/LICENSE")
                for name in wheel_members
            ):
                raise ValueError("wheel missing license file")

    with stable_file(sdist, MAX_ARCHIVE_SOURCE_BYTES, "source distribution") as (
        sdist_stream,
        _sdist_size,
    ):
        decompress_sdist(sdist_stream, raw_tar, "source distribution")
    with stable_file(raw_tar, MAX_TAR_BYTES, "expanded source distribution") as (
        raw_tar_stream,
        raw_tar_size,
    ):
        preflight_tar(raw_tar_stream, raw_tar_size, "source distribution")
        sdist_members = bounded_tar_inventory(
            raw_tar_stream, "source distribution"
        )
    prefixes = {name.split("/", 1)[0] for name in sdist_members if "/" in name}
    if len(prefixes) != 1:
        raise ValueError("sdist must contain exactly one top-level package prefix")
    prefix = next(iter(prefixes))
    required_sdist = {
        f"{prefix}/CODE_OF_CONDUCT.md",
        f"{prefix}/SECURITY.md",
        f"{prefix}/SUPPORT.md",
        f"{prefix}/docs/getting-started.md",
        f"{prefix}/spec/HASHING.md",
        f"{prefix}/spec/cli-output.plan.schema.json",
        f"{prefix}/spec/cli-output.review.schema.json",
        f"{prefix}/spec/cli-output.slice.schema.json",
        f"{prefix}/spec/cli-output.why.schema.json",
    }
    missing_sdist = required_sdist - sdist_members
    if missing_sdist:
        raise ValueError(f"sdist missing: {sorted(missing_sdist)}")
    for internal in (
        f"{prefix}/docs/PROJECT_REVIEW.md",
        f"{prefix}/docs/RELEASING.md",
        f"{prefix}/docs/documentation-style.md",
        f"{prefix}/docs/hashing-contract.md",
        f"{prefix}/docs/specification.md",
        f"{prefix}/docs/support.md",
        f"{prefix}/tests",
        f"{prefix}/scripts",
        f"{prefix}/.github",
        f"{prefix}/.pre-commit-hooks.yaml",
        f"{prefix}/Dockerfile",
        f"{prefix}/action.yml",
    ):
        if internal in sdist_members or any(
            name.startswith(internal + "/") for name in sdist_members
        ):
            raise ValueError(f"sdist contains repository-only material: {internal}")

    with stable_file(pyz, MAX_ARCHIVE_SOURCE_BYTES, "standalone archive") as (
        pyz_stream,
        pyz_size,
    ):
        preflight_zip(pyz_stream, pyz_size, "standalone archive")
        with ZipFile(pyz_stream) as archive:
            pyz_members, pyz_infos = bounded_zip_inventory(
                archive, "standalone archive"
            )
            metadata_infos = [
                info
                for info in pyz_infos
                if info.filename.endswith(".dist-info/METADATA")
            ]
            if len(metadata_infos) != 1:
                raise ValueError(
                    "standalone archive must contain exactly one METADATA member"
                )
            metadata = email.parser.BytesParser().parsebytes(
                read_zip_member(
                    archive,
                    metadata_infos[0],
                    MAX_METADATA_BYTES,
                    "standalone METADATA",
                )
            )
            if (
                metadata["Name"] != "boundver"
                or metadata["Version"] != expected_version
            ):
                raise ValueError("standalone distribution metadata mismatch")
            license_infos = [
                info for info in pyz_infos if info.filename == "LICENSE"
            ]
            if len(license_infos) != 1 or read_zip_member(
                archive,
                license_infos[0],
                MAX_LICENSE_BYTES,
                "standalone LICENSE",
            ) != license_bytes:
                raise ValueError(
                    "standalone archive license does not match repository LICENSE"
                )
            if not any(
                name.endswith(".dist-info/licenses/LICENSE")
                for name in pyz_members
            ):
                raise ValueError("standalone archive missing distribution license")
            if metadata.get_all("License-File") != [
                "licenses/LICENSE",
                "licenses/PyYAML-LICENSE",
            ]:
                raise ValueError("standalone license metadata is incomplete")
            metadata_prefix = metadata_infos[0].filename.removesuffix("METADATA")
            vendored_infos = [
                info
                for info in pyz_infos
                if info.filename == metadata_prefix + "VENDORED"
            ]
            if len(vendored_infos) != 1 or read_zip_member(
                archive,
                vendored_infos[0],
                MAX_METADATA_BYTES,
                "standalone vendored dependency record",
            ) != f"PyYAML=={pyyaml_version}\n".encode("ascii"):
                raise ValueError("standalone PyYAML version record is not lock-bound")
            required_pyyaml = {
                "yaml/__init__.py",
                "yaml/composer.py",
                "yaml/constructor.py",
                "yaml/cyaml.py",
                "yaml/dumper.py",
                "yaml/emitter.py",
                "yaml/error.py",
                "yaml/events.py",
                "yaml/loader.py",
                "yaml/nodes.py",
                "yaml/parser.py",
                "yaml/reader.py",
                "yaml/representer.py",
                "yaml/resolver.py",
                "yaml/scanner.py",
                "yaml/serializer.py",
                "yaml/tokens.py",
            }
            if not required_pyyaml.issubset(pyz_members):
                raise ValueError("standalone archive is missing pure-Python PyYAML")
            if any(
                name.startswith("yaml/_yaml.")
                and name.endswith((".pyd", ".so"))
                for name in pyz_members
            ):
                raise ValueError("standalone archive contains a platform native module")
            pyyaml_license_infos = [
                info
                for info in pyz_infos
                if info.filename
                == metadata_prefix + "licenses/PyYAML-LICENSE"
            ]
            if len(pyyaml_license_infos) != 1 or not read_zip_member(
                archive,
                pyyaml_license_infos[0],
                MAX_LICENSE_BYTES,
                "standalone PyYAML license",
            ):
                raise ValueError("standalone archive has an empty PyYAML license")
except (
    BadZipFile,
    EOFError,
    KeyError,
    OSError,
    RuntimeError,
    UnicodeError,
    ValueError,
    tarfile.TarError,
) as error:
    raise SystemExit(f"Bounded packaging inspection failed: {error}") from error
finally:
    try:
        raw_tar.unlink()
    except FileNotFoundError:
        pass
PY

wheel_venv="$smoke_root/w"
sdist_venv="$smoke_root/s"
standalone_venv="$smoke_root/a"
python -I -m venv "$wheel_venv"
python -I -m venv "$sdist_venv"
python -I -m venv "$standalone_venv"

resolved_venv_python=
resolve_venv_python() {
  local environment=$1
  local candidate
  resolved_venv_python=
  for candidate in "$environment/bin/python" "$environment/Scripts/python.exe"; do
    if [[ -f "$candidate" ]]; then
      resolved_venv_python=$candidate
      return 0
    fi
  done
  echo "Virtual environment has no Python executable: $environment" >&2
  return 1
}

resolve_venv_python "$wheel_venv"
wheel_python=$resolved_venv_python
resolve_venv_python "$sdist_venv"
sdist_python=$resolved_venv_python
resolve_venv_python "$standalone_venv"
standalone_python=$resolved_venv_python
"$wheel_python" -I -m pip --isolated install \
  --no-index --no-deps "$wheel_path"
"$sdist_python" -I "$PWD/scripts/install_locked_tools.py" action
"$sdist_python" -I -m pip --isolated install \
  --no-index --no-deps --no-build-isolation "$sdist_path"

repo="$smoke_root/r"
mkdir "$repo"
git -C "$repo" init -q
git -C "$repo" config user.email smoke@example.com
git -C "$repo" config user.name "Packaging Smoke"
mkdir "$repo/svc"
printf '{"contract": 1}\n' > "$repo/svc/api.json"
printf '%s\n' \
  '{' \
  '  "project": "packaging-smoke",' \
  '  "components": {' \
  '    "svc": {' \
  '      "path": "svc",' \
  '      "version_source": null,' \
  '      "boundary": {"provider": "json-file", "paths": ["api.json"]}' \
  '    }' \
  '  },' \
  '  "slices": {}' \
  '}' > "$repo/boundary.config.json"
git -C "$repo" add svc/api.json boundary.config.json
git -C "$repo" commit -qm baseline

run_installed_smoke() {
  local runtime=$1
  "$runtime" -I -m boundver --version | grep -F " $expected_version" >/dev/null
  "$runtime" -I -m boundver validate-config
  "$runtime" -I -m boundver generate --source head --format json >/dev/null
  "$runtime" -I -m boundver verify --source head --format json >/dev/null
  "$runtime" -I -m boundver status --source head --format json >/dev/null
}

cd "$repo"
# Bootstrap and commit the lock once. Head/index verification intentionally
# reads its lock from that selected Git snapshot, not from an unstaged file
# just written by generate.
"$wheel_python" -I -m boundver validate-config
"$wheel_python" -I -m boundver generate --source head --format json >/dev/null
git add boundary.lock.json
git commit -qm "record lock"
run_installed_smoke "$wheel_python"
run_installed_smoke "$sdist_python"
"$standalone_python" -I "$pyz_path" --version | grep -F " $expected_version" >/dev/null
"$standalone_python" -I "$pyz_path" validate-config
"$standalone_python" -I "$pyz_path" generate --source head --format json >/dev/null
"$standalone_python" -I "$pyz_path" verify --source head --format json >/dev/null

# The standalone/Homebrew artifact must carry its own YAML runtime. Prove that
# a clean interpreter with no installed PyYAML can load a YAML config, extract a
# YAML version source, and canonicalize a YAML OpenAPI boundary.
if "$standalone_python" -I -c "import yaml" >/dev/null 2>&1; then
  echo "Standalone smoke environment unexpectedly provides external PyYAML" >&2
  exit 1
fi
yaml_repo="$smoke_root/y"
mkdir "$yaml_repo"
git -C "$yaml_repo" init -q
git -C "$yaml_repo" config user.email smoke@example.com
git -C "$yaml_repo" config user.name "Standalone YAML Smoke"
mkdir "$yaml_repo/service"
printf '%s\n' \
  'project: standalone-yaml-smoke' \
  'components:' \
  '  api:' \
  '    path: service' \
  '    version_source:' \
  '      file: openapi.yaml' \
  '      field: info.version' \
  '    boundary:' \
  '      provider: openapi-canonical' \
  '      paths: [openapi.yaml]' \
  'slices: {}' > "$yaml_repo/boundary.config.yaml"
printf '%s\n' \
  'openapi: 3.0.0' \
  'info:' \
  '  title: Standalone smoke' \
  '  version: 1.2.3' \
  'paths:' \
  '  /health:' \
  '    get:' \
  '      responses:' \
  "        '200':" \
  '          description: OK' > "$yaml_repo/service/openapi.yaml"
git -C "$yaml_repo" add boundary.config.yaml service/openapi.yaml
git -C "$yaml_repo" commit -qm fixture
cd "$yaml_repo"
"$standalone_python" -I "$pyz_path" validate-config
"$standalone_python" -I "$pyz_path" generate --source head --format json >/dev/null
"$standalone_python" -I - <<'PY'
import json
from pathlib import Path

lock = json.loads(Path("boundary.lock.json").read_text(encoding="utf-8"))
if lock["components"]["api"]["version"] != "1.2.3":
    raise SystemExit("standalone archive did not extract the YAML version source")
PY
git add boundary.lock.json
git commit -qm lock
"$standalone_python" -I "$pyz_path" verify --source head --format json >/dev/null
"$standalone_python" -I - "$pyz_path" "$expected_version" <<'PY'
import sys

sys.path.insert(0, sys.argv[1])
import boundver

if boundver.__version__ != sys.argv[2]:
    raise SystemExit(
        f"standalone package version {boundver.__version__!r} != {sys.argv[2]!r}"
    )
PY
