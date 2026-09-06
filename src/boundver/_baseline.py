"""Versioned verification baselines for explicit debt ratcheting.

Baselines store structured violation identities, never hash-bearing diagnostic
strings.  They are intentionally separate from the lockfile: a lock describes
reviewed repository state, while a baseline temporarily acknowledges known
verification debt so CI can reject only newly introduced violations.
"""

import hashlib
import io
import json
import os
import re
import secrets
import stat
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from ._config import _snapshot_relative_path
from ._git import GitSourceSnapshot, _git_cat_blob
from ._hashing import (
    _capture_working_tree_ancestors,
    _read_bounded_path_bytes,
    _verify_working_tree_ancestors,
)
from ._utils import (
    FACET_SET,
    SOURCE_MODE_SET,
    ConfigError,
    GuardrailError,
    _bounded_diagnostic_repr,
    _bounded_json_dumps,
    _bounded_json_float,
    _bounded_json_int,
    _bounded_json_value_issues,
    _is_windows_reparse_point,
)


BASELINE_SCHEMA = "boundver-verify-baseline/v1"
BASELINE_SCHEMA_URL = (
    "https://raw.githubusercontent.com/yzm1/boundver/v0.15.1/"
    "spec/verify-baseline.schema.json"
)
MAX_BASELINE_BYTES = 2 * 1024 * 1024
MAX_BASELINE_VIOLATIONS = 10_000
MAX_BASELINE_TEXT = 4_096


class BaselineError(ValueError):
    """Raised when a verification baseline is unsafe or incompatible."""


def _validate_baseline_relative_path(relative: Path) -> None:
    """Reject path spellings that cannot identify one portable repository file.

    A colon inside a repository-relative component selects an NTFS alternate
    data stream on Windows.  Such a stream passes ordinary regular-file and
    suffix checks but cannot be represented as a tracked Git file, so allowing
    it would let a baseline be hidden behind another pathname.
    """
    if any(":" in part for part in relative.parts):
        raise BaselineError(
            "verification baseline paths must not use ':' "
            "(NTFS alternate-data-stream syntax)"
        )


_COMPONENT_FACET_RE = re.compile(
    r"^MISMATCH (?P<subject>.+)\.(?P<facet>exact|behavior|boundary|compat):",
    re.DOTALL,
)
_SLICE_FACET_RE = re.compile(
    r"^SLICE MISMATCH (?P<subject>.+)\.(?P<facet>exact|behavior|boundary|compat):",
    re.DOTALL,
)
_CONSUMERS_RE = re.compile(
    r"^AFFECTED CONSUMERS(?P<transitive> \(TRANSITIVE\))? "
    r"(?P<body>.+)$",
    re.DOTALL,
)
_ANCILLARY_COMPAT_METADATA_RE = re.compile(
    r"^METADATA MISMATCH (?P<subject>.+)\.(?:version|semver):",
    re.DOTALL,
)
_KIND_FACETS = {
    "component-facet": FACET_SET,
    "slice-facet": FACET_SET,
    "affected-consumers": {"direct", "transitive"},
}


def _identity_entry(kind: str, subject: str, facet: Optional[str]) -> dict:
    if kind not in _KIND_FACETS:
        raise BaselineError(f"unknown verification baseline violation kind: {kind!r}")
    if facet not in _KIND_FACETS[kind]:
        raise BaselineError(
            f"invalid facet {facet!r} for verification baseline kind {kind!r}"
        )
    if not subject or len(subject) > MAX_BASELINE_TEXT:
        raise BaselineError("verification issue subject exceeds the baseline limit")
    identity = "\0".join((BASELINE_SCHEMA, kind, subject, facet or ""))
    return {
        "id": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        "kind": kind,
        "subject": subject,
        "facet": facet,
    }


def violation_identity(
    issue: str,
    *,
    component_subjects: Optional[set] = None,
) -> Optional[dict]:
    """Return a stable structured identity for one baselinable drift issue.

    Integrity, configuration, digest-computation, and unavailable-facet
    failures deliberately return ``None``.  Those failures must be repaired;
    a ratchet may not acknowledge them.
    """
    match = _COMPONENT_FACET_RE.match(issue)
    if match:
        return _identity_entry(
            "component-facet", match.group("subject"), match.group("facet")
        )
    match = _SLICE_FACET_RE.match(issue)
    if match:
        return _identity_entry(
            "slice-facet", match.group("subject"), match.group("facet")
        )
    match = _CONSUMERS_RE.match(issue)
    if match and component_subjects:
        body = match.group("body")
        # Consumer names and component names may both contain ':'. Bind the
        # diagnostic to an exact component that has a facet mismatch in this
        # issue set instead of splitting an untrusted display string at ':'.
        subjects = sorted(component_subjects, key=lambda item: (-len(item), item))
        subject = next(
            (candidate for candidate in subjects if body.startswith(candidate + ": ")),
            None,
        )
        if subject is None:
            return None
        return _identity_entry(
            "affected-consumers",
            subject,
            "transitive" if match.group("transitive") else "direct",
        )
    return None


def _issues_with_identities(issues: List[str]) -> List[Tuple[str, Optional[dict]]]:
    identified: List[Tuple[str, Optional[dict]]] = []
    pending_component_subject: Optional[str] = None
    for issue in issues:
        identity = violation_identity(issue)
        if identity is not None and identity["kind"] == "component-facet":
            pending_component_subject = identity["subject"]
        elif _CONSUMERS_RE.match(issue) is not None:
            subjects = (
                {pending_component_subject}
                if pending_component_subject is not None
                else set()
            )
            identity = violation_identity(issue, component_subjects=subjects)
            pending_component_subject = None
        else:
            # Consumer diagnostics are emitted immediately after their owning
            # component mismatch. Resetting on every other issue makes that
            # structural association authoritative even when names and the
            # rendered consumer list contain indistinguishable ': ' text.
            pending_component_subject = None
        identified.append((issue, identity))
    return identified


def _ancillary_compat_subject(issue: str) -> Optional[str]:
    match = _ANCILLARY_COMPAT_METADATA_RE.match(issue)
    if match is None:
        return None
    subject = match.group("subject")
    if not subject or len(subject) > MAX_BASELINE_TEXT:
        return None
    return subject


def _compat_subjects(issues: List[str]) -> set:
    subjects = set()
    for _issue, identity in _issues_with_identities(issues):
        if (
            identity is not None
            and identity["kind"] == "component-facet"
            and identity["facet"] == "compat"
        ):
            subjects.add(identity["subject"])
    return subjects


def _strict_object(pairs: List[tuple]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise BaselineError(
                f"duplicate JSON object key {_bounded_diagnostic_repr(key)}"
            )
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise BaselineError(f"non-finite JSON number {value!r} is not supported")


def _string(value: object, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise BaselineError(f"baseline field {field!r} must be a string")
    if len(value) > MAX_BASELINE_TEXT:
        raise BaselineError(f"baseline field {field!r} exceeds the text limit")
    return value


def validate_baseline(value: object) -> dict:
    if not isinstance(value, dict):
        raise BaselineError("verification baseline root must be an object")
    expected = {
        "$schema",
        "schema",
        "project",
        "lock_schema",
        "lock_digest",
        "config_contract",
        "source",
        "components_filter",
        "facets",
        "transitive",
        "policy_digest",
        "violations",
    }
    unknown = sorted(key for key in value if key not in expected)
    missing = sorted(key for key in expected if key not in value)
    if unknown:
        raise BaselineError(
            "unknown baseline fields: " + _bounded_diagnostic_repr(unknown)
        )
    if missing:
        raise BaselineError("missing baseline fields: " + ", ".join(missing))
    if value["$schema"] != BASELINE_SCHEMA_URL:
        raise BaselineError(
            "verification baseline $schema is unsupported; regenerate it with "
            "this Boundver version"
        )
    if value["schema"] != BASELINE_SCHEMA:
        raise BaselineError(f"verification baseline schema must be {BASELINE_SCHEMA!r}")
    for field in (
        "project",
        "lock_schema",
        "lock_digest",
        "config_contract",
        "source",
        "policy_digest",
    ):
        _string(value[field], field)
    if value["source"] not in SOURCE_MODE_SET:
        raise BaselineError("baseline source must be head, index, or working-tree")
    components_filter = value["components_filter"]
    if (
        not isinstance(components_filter, list)
        or len(components_filter) > MAX_BASELINE_VIOLATIONS
        or any(
            not isinstance(item, str) or not item or len(item) > MAX_BASELINE_TEXT
            for item in components_filter
        )
        or components_filter != sorted(set(components_filter))
    ):
        raise BaselineError(
            "baseline components_filter must be a sorted unique string array"
        )
    facets = value["facets"]
    if facets is not None and (
        not isinstance(facets, list)
        or any(not isinstance(item, str) or item not in FACET_SET for item in facets)
        or facets != sorted(set(facets))
    ):
        raise BaselineError(
            "baseline facets must be null or a sorted unique facet array"
        )
    if not isinstance(value["transitive"], bool):
        raise BaselineError("baseline transitive must be a boolean")
    if re.fullmatch(r"[0-9a-f]{64}", value["policy_digest"]) is None:
        raise BaselineError("baseline policy_digest must be a SHA-256 digest")
    if re.fullmatch(r"[0-9a-f]{64}", value["lock_digest"]) is None:
        raise BaselineError("baseline lock_digest must be a SHA-256 digest")
    violations = value["violations"]
    if not isinstance(violations, list):
        raise BaselineError("baseline violations must be an array")
    if len(violations) > MAX_BASELINE_VIOLATIONS:
        raise BaselineError(
            f"baseline exceeds the {MAX_BASELINE_VIOLATIONS}-violation limit"
        )
    validated: List[dict] = []
    seen = set()
    for index, entry in enumerate(violations):
        if not isinstance(entry, dict):
            raise BaselineError(f"baseline violations[{index}] must be an object")
        if set(entry) != {"id", "kind", "subject", "facet"}:
            raise BaselineError(
                f"baseline violations[{index}] has missing or unknown fields"
            )
        identity = _identity_entry(
            _string(entry["kind"], f"violations[{index}].kind"),
            _string(entry["subject"], f"violations[{index}].subject"),
            (
                None
                if entry["facet"] is None
                else _string(entry["facet"], f"violations[{index}].facet")
            ),
        )
        if entry["id"] != identity["id"]:
            raise BaselineError(
                f"baseline violations[{index}].id does not match its identity"
            )
        if identity["id"] in seen:
            raise BaselineError("baseline contains duplicate violation identities")
        seen.add(identity["id"])
        validated.append(identity)
    if validated != sorted(validated, key=lambda item: item["id"]):
        raise BaselineError("baseline violations must be sorted by id")
    return value


def _parse_baseline_bytes(raw: bytes, path_label: object) -> dict:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BaselineError(
            f"verification baseline is not UTF-8: {path_label}"
        ) from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_float=_bounded_json_float,
            parse_int=_bounded_json_int,
            parse_constant=_reject_constant,
        )
    except (ValueError, RecursionError, OverflowError) as exc:
        raise BaselineError(
            f"invalid verification baseline JSON at {path_label}: {exc}"
        ) from exc
    try:
        tree_issues = _bounded_json_value_issues(value, path="baseline")
    except (GuardrailError, RecursionError, RuntimeError, ValueError) as exc:
        raise BaselineError(
            f"verification baseline cannot be traversed safely: {exc}"
        ) from exc
    if tree_issues:
        raise BaselineError("unsafe verification baseline: " + tree_issues[0])
    return validate_baseline(value)


def load_baseline_with_bytes(
    path: Path,
    *,
    repo_root: Optional[Path] = None,
    snapshot: Optional[GitSourceSnapshot] = None,
) -> Tuple[dict, bytes]:
    """Load a baseline and the exact bounded bytes selected for the operation."""
    if snapshot is not None:
        if repo_root is None:
            raise BaselineError(
                "repo_root is required for source-backed baseline reads"
            )
        try:
            label = _snapshot_relative_path(repo_root, path)
        except (ConfigError, ValueError, OSError) as exc:
            raise BaselineError(
                "verification baseline path must stay within the repository"
            ) from exc
        entry = snapshot.entries.get(label)
        if entry is None:
            raise BaselineError(
                f"verification baseline not found in captured {snapshot.source} "
                f"source: {label}"
            )
        if entry.object_type != "blob" or entry.mode not in {"100644", "100755"}:
            raise BaselineError(
                "verification baseline path must be a regular file in captured "
                f"{snapshot.source} source: {label} "
                f"(mode={entry.mode}, type={entry.object_type})"
            )
        try:
            raw = _git_cat_blob(
                repo_root,
                entry.oid,
                max_bytes=MAX_BASELINE_BYTES,
            )
        except GuardrailError as exc:
            raise BaselineError(
                "cannot read verification baseline from captured "
                f"{snapshot.source} source: {label}: file too large or "
                "transport limit exceeded"
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise BaselineError(
                "cannot read verification baseline from captured "
                f"{snapshot.source} source: {label}"
            ) from exc
        return _parse_baseline_bytes(raw, label), raw
    ancestors = []
    path_label = str(path)
    if repo_root is not None:
        lexical_root = Path(os.path.abspath(repo_root))
        lexical_path = path if path.is_absolute() else lexical_root / path
        lexical_path = Path(os.path.abspath(lexical_path))
        try:
            path_label = _snapshot_relative_path(lexical_root, lexical_path)
            ancestors = _capture_working_tree_ancestors(
                lexical_root, lexical_path, path_label
            )
        except (ConfigError, OSError, ValueError) as exc:
            raise BaselineError(
                f"unsafe verification baseline path {path}: {exc}"
            ) from exc
    try:
        raw = _read_bounded_path_bytes(path, str(path), max_bytes=MAX_BASELINE_BYTES)
    except FileNotFoundError as exc:
        raise BaselineError(f"verification baseline not found: {path}") from exc
    except (OSError, ValueError) as exc:
        raise BaselineError(f"cannot read verification baseline {path}: {exc}") from exc
    if ancestors:
        try:
            _verify_working_tree_ancestors(ancestors, path_label)
        except (OSError, ValueError) as exc:
            raise BaselineError(
                f"unsafe verification baseline path {path}: {exc}"
            ) from exc
    return _parse_baseline_bytes(raw, path), raw


def load_baseline(
    path: Path,
    *,
    repo_root: Optional[Path] = None,
    snapshot: Optional[GitSourceSnapshot] = None,
) -> dict:
    """Load a baseline from disk or the operation's captured Git source."""
    baseline, _raw = load_baseline_with_bytes(
        path,
        repo_root=repo_root,
        snapshot=snapshot,
    )
    return baseline


def _baseline_payload_bytes(text: str) -> bytes:
    if not isinstance(text, str):
        raise BaselineError("verification baseline output must be text")
    payload = text.encode("utf-8")
    if len(payload) > MAX_BASELINE_BYTES:
        raise BaselineError(
            f"verification baseline exceeds the {MAX_BASELINE_BYTES}-byte storage limit"
        )
    return payload


def _open_windows_path_no_follow(
    path: Path,
    *,
    directory: bool,
    readable: bool,
    deny_delete: bool,
) -> int:
    """Open one Windows root path without traversing a final reparse point."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    desired_access = 0x80000000 if readable else 0  # GENERIC_READ
    share_mode = 0x00000001 | 0x00000002  # FILE_SHARE_READ | FILE_SHARE_WRITE
    if not deny_delete:
        share_mode |= 0x00000004  # FILE_SHARE_DELETE
    flags = 0x00200000  # FILE_FLAG_OPEN_REPARSE_POINT
    if directory:
        flags |= 0x02000000  # FILE_FLAG_BACKUP_SEMANTICS
    handle = create_file(
        str(path),
        desired_access,
        share_mode,
        None,
        3,  # OPEN_EXISTING
        flags,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle in (None, invalid_handle):
        raise ctypes.WinError(ctypes.get_last_error())
    open_flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        open_flags |= os.O_BINARY
    try:
        fd = msvcrt.open_osfhandle(handle, open_flags)
    except BaseException:
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)
        raise
    try:
        identity = os.fstat(fd)
        expected_type = (
            stat.S_ISDIR(identity.st_mode)
            if directory
            else stat.S_ISREG(identity.st_mode)
        )
        if not expected_type or _is_windows_reparse_point(identity):
            kind = "directory" if directory else "regular file"
            raise ValueError(f"path is not a plain {kind}: {path}")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _raise_windows_ntstatus(status: int) -> None:
    import ctypes
    from ctypes import wintypes

    ntdll = ctypes.WinDLL("ntdll")
    convert = ntdll.RtlNtStatusToDosError
    convert.argtypes = [ctypes.c_long]
    convert.restype = wintypes.ULONG
    raise ctypes.WinError(convert(status))


def _open_windows_relative(
    directory_fd: int,
    name: str,
    *,
    directory: bool,
    readable: bool = False,
    writable: bool = False,
    delete: bool = False,
    create: bool = False,
) -> int:
    """Open or exclusively create *name* relative to a held NT directory."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _UnicodeString(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", wintypes.LPWSTR),
        ]

    class _ObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.ULONG),
            ("RootDirectory", wintypes.HANDLE),
            ("ObjectName", ctypes.POINTER(_UnicodeString)),
            ("Attributes", wintypes.ULONG),
            ("SecurityDescriptor", wintypes.LPVOID),
            ("SecurityQualityOfService", wintypes.LPVOID),
        ]

    class _IoStatusBlock(ctypes.Structure):
        _fields_ = [
            ("Status", ctypes.c_void_p),
            ("Information", ctypes.c_size_t),
        ]

    encoded_name = name.encode("utf-16-le")
    if len(encoded_name) > 0xFFFC:
        raise ValueError("Windows baseline filename exceeds the NT text limit")
    name_buffer = ctypes.create_unicode_buffer(name)
    unicode_name = _UnicodeString(
        len(encoded_name),
        len(encoded_name) + 2,
        ctypes.cast(name_buffer, wintypes.LPWSTR),
    )
    attributes = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes),
        msvcrt.get_osfhandle(directory_fd),
        ctypes.pointer(unicode_name),
        0x00000040,  # OBJ_CASE_INSENSITIVE
        None,
        None,
    )
    desired_access = 0x00100080  # SYNCHRONIZE | FILE_READ_ATTRIBUTES
    if directory:
        desired_access |= 0x00000020  # FILE_TRAVERSE
    if readable:
        desired_access |= 0x00020009  # READ_CONTROL | READ_DATA | READ_EA
    if writable:
        desired_access |= 0x00020116  # FILE_GENERIC_WRITE without SYNCHRONIZE
    if delete:
        desired_access |= 0x00010000  # DELETE
    create_options = 0x00000020 | 0x00200000
    create_options |= 0x00000001 if directory else 0x00000040
    io_status = _IoStatusBlock()
    handle = wintypes.HANDLE()
    ntdll = ctypes.WinDLL("ntdll")
    create_file = ntdll.NtCreateFile
    create_file.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_IoStatusBlock),
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    create_file.restype = ctypes.c_long
    status = create_file(
        ctypes.byref(handle),
        desired_access,
        ctypes.byref(attributes),
        ctypes.byref(io_status),
        None,
        0x00000010 if directory else 0x00000080,
        0x00000001 | 0x00000002 | 0x00000004,
        2 if create else 1,  # FILE_CREATE or FILE_OPEN
        create_options,
        None,
        0,
    )
    if status < 0:
        _raise_windows_ntstatus(status)
    open_flags = os.O_WRONLY if writable else os.O_RDONLY
    open_flags |= getattr(os, "O_BINARY", 0)
    try:
        fd = msvcrt.open_osfhandle(handle.value, open_flags)
    except BaseException:
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)
        raise
    try:
        identity = os.fstat(fd)
        expected_type = (
            stat.S_ISDIR(identity.st_mode)
            if directory
            else stat.S_ISREG(identity.st_mode)
        )
        if not expected_type or _is_windows_reparse_point(identity):
            kind = "directory" if directory else "regular file"
            raise ValueError(f"path is not a plain {kind}: {name}")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _windows_set_sibling_name(
    directory_fd: int,
    source: str,
    target: str,
    *,
    information_class: int,
    replace: bool,
) -> None:
    """Apply one NT link/rename operation relative to *directory_fd*."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _IoStatusBlock(ctypes.Structure):
        _fields_ = [
            ("Status", ctypes.c_void_p),
            ("Information", ctypes.c_size_t),
        ]

    class _NameInformation(ctypes.Structure):
        _fields_ = [
            ("ReplaceIfExists", wintypes.BOOLEAN),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * 1),
        ]

    source_fd = _open_windows_relative(
        directory_fd,
        source,
        directory=False,
        delete=information_class == 10,
    )
    try:
        encoded_target = target.encode("utf-16-le")
        name_offset = _NameInformation.FileName.offset
        buffer_size = max(
            ctypes.sizeof(_NameInformation),
            name_offset + len(encoded_target),
        )
        buffer = ctypes.create_string_buffer(buffer_size)
        information = _NameInformation.from_buffer(buffer)
        information.ReplaceIfExists = replace
        information.RootDirectory = msvcrt.get_osfhandle(directory_fd)
        information.FileNameLength = len(encoded_target)
        ctypes.memmove(
            ctypes.addressof(buffer) + name_offset,
            encoded_target,
            len(encoded_target),
        )
        io_status = _IoStatusBlock()
        ntdll = ctypes.WinDLL("ntdll")
        set_information = ntdll.NtSetInformationFile
        set_information.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_IoStatusBlock),
            ctypes.c_void_p,
            wintypes.ULONG,
            ctypes.c_int,
        ]
        set_information.restype = ctypes.c_long
        status = set_information(
            msvcrt.get_osfhandle(source_fd),
            ctypes.byref(io_status),
            buffer,
            name_offset + len(encoded_target),
            information_class,
        )
        if status < 0:
            _raise_windows_ntstatus(status)
    finally:
        os.close(source_fd)


def _windows_unlink_sibling(directory_fd: int, name: str) -> None:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _IoStatusBlock(ctypes.Structure):
        _fields_ = [
            ("Status", ctypes.c_void_p),
            ("Information", ctypes.c_size_t),
        ]

    class _DispositionInformation(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOLEAN)]

    file_fd = _open_windows_relative(
        directory_fd,
        name,
        directory=False,
        delete=True,
    )
    try:
        information = _DispositionInformation(True)
        io_status = _IoStatusBlock()
        ntdll = ctypes.WinDLL("ntdll")
        set_information = ntdll.NtSetInformationFile
        set_information.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_IoStatusBlock),
            ctypes.c_void_p,
            wintypes.ULONG,
            ctypes.c_int,
        ]
        set_information.restype = ctypes.c_long
        status = set_information(
            msvcrt.get_osfhandle(file_fd),
            ctypes.byref(io_status),
            ctypes.byref(information),
            ctypes.sizeof(information),
            13,  # FileDispositionInformation
        )
        if status < 0:
            _raise_windows_ntstatus(status)
    finally:
        os.close(file_fd)


def _open_plain_directory(path: Path) -> int:
    if os.name == "nt":
        return _open_windows_path_no_follow(
            path,
            directory=True,
            readable=False,
            deny_delete=True,
        )
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return os.open(str(path), flags)


def _open_plain_child_directory(
    directory_fd: int,
    name: str,
    *,
    create: bool,
) -> int:
    """Open or create one plain child relative to a held directory handle."""
    if (
        not name
        or "\0" in name
        or ":" in name
        or name in {".", ".."}
        or Path(name).name != name
    ):
        raise ValueError("directory traversal requires a child directory name")

    if os.name == "nt":
        try:
            return _open_windows_relative(
                directory_fd,
                name,
                directory=True,
            )
        except FileNotFoundError:
            if not create:
                raise
            try:
                return _open_windows_relative(
                    directory_fd,
                    name,
                    directory=True,
                    create=True,
                )
            except FileExistsError:
                # Another process won the create race. Re-open without
                # following a reparse point and validate what now exists.
                return _open_windows_relative(
                    directory_fd,
                    name,
                    directory=True,
                )

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        child_fd = os.open(name, flags, dir_fd=directory_fd)
    except FileNotFoundError:
        if not create:
            raise
        try:
            os.mkdir(name, mode=0o777, dir_fd=directory_fd)
        except FileExistsError:
            pass
        child_fd = os.open(name, flags, dir_fd=directory_fd)
    try:
        identity = os.fstat(child_fd)
        if not stat.S_ISDIR(identity.st_mode) or _is_windows_reparse_point(identity):
            raise ValueError(f"path is not a plain directory: {name}")
    except BaseException:
        os.close(child_fd)
        raise
    return child_fd


def _same_directory_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(right.st_mode)
        and not _is_windows_reparse_point(right)
        and (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)
    )


class _MutationDirectory:
    """A validated parent directory held for one complete baseline mutation."""

    def __init__(self, path: Path, fd: int) -> None:
        self.path = path
        self.fd = fd

    @staticmethod
    def _validate_name(name: str) -> None:
        if (
            not name
            or "\0" in name
            or ":" in name
            or name in {".", ".."}
            or Path(name).name != name
        ):
            raise ValueError("baseline mutation requires a sibling filename")

    def open_exclusive(self, name: str, mode: int = 0o600) -> int:
        self._validate_name(name)
        if os.name == "nt":
            return _open_windows_relative(
                self.fd,
                name,
                directory=False,
                writable=True,
                create=True,
            )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_BINARY", 0)
        return os.open(name, flags, mode, dir_fd=self.fd)

    def open_read(self, name: str) -> int:
        self._validate_name(name)
        if os.name == "nt":
            return _open_windows_relative(
                self.fd,
                name,
                directory=False,
                readable=True,
            )
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_BINARY", 0)
        return os.open(name, flags, dir_fd=self.fd)

    def lstat(self, name: str) -> os.stat_result:
        self._validate_name(name)
        if os.name == "nt":
            fd = _open_windows_relative(
                self.fd,
                name,
                directory=False,
            )
            try:
                return os.fstat(fd)
            finally:
                os.close(fd)
        return os.stat(name, dir_fd=self.fd, follow_symlinks=False)

    def link(self, source: str, target: str) -> None:
        self._validate_name(source)
        self._validate_name(target)
        if os.name == "nt":
            _windows_set_sibling_name(
                self.fd,
                source,
                target,
                information_class=11,  # FileLinkInformation
                replace=False,
            )
            return
        os.link(
            source,
            target,
            src_dir_fd=self.fd,
            dst_dir_fd=self.fd,
            follow_symlinks=False,
        )

    def replace(self, source: str, target: str) -> None:
        self._validate_name(source)
        self._validate_name(target)
        if os.name == "nt":
            _windows_set_sibling_name(
                self.fd,
                source,
                target,
                information_class=10,  # FileRenameInformation
                replace=True,
            )
            return
        os.replace(
            source,
            target,
            src_dir_fd=self.fd,
            dst_dir_fd=self.fd,
        )

    def unlink(self, name: str) -> None:
        self._validate_name(name)
        if os.name == "nt":
            _windows_unlink_sibling(self.fd, name)
            return
        os.unlink(name, dir_fd=self.fd)

    def fsync(self) -> None:
        if os.name != "nt":
            os.fsync(self.fd)


@contextmanager
def _mutation_directory(
    path: Path,
    repo_root: Path,
    *,
    create_parents: bool,
) -> Iterator[Tuple[str, _MutationDirectory]]:
    """Yield a no-follow, identity-bound parent for a repository target."""
    lexical_root = Path(os.path.abspath(repo_root))
    lexical_path = Path(os.path.abspath(path))
    held_fds: List[int] = []
    try:
        label = _snapshot_relative_path(lexical_root, lexical_path)
        relative = lexical_path.relative_to(lexical_root)
        if not relative.parts or relative.name in {"", ".", ".."}:
            raise ValueError("verification baseline path must name a file")
        _validate_baseline_relative_path(relative)

        root_before = lexical_root.lstat()
        root_fd = _open_plain_directory(lexical_root)
        held_fds.append(root_fd)
        if not _same_directory_identity(root_before, os.fstat(root_fd)):
            raise ValueError("repository root changed while opening baseline path")

        current_path = lexical_root
        current_fd = root_fd
        for part in relative.parts[:-1]:
            if part in {"", ".", ".."}:
                raise ValueError("verification baseline path escapes the repository")
            child_path = current_path / part
            child_fd = _open_plain_child_directory(
                current_fd,
                part,
                create=create_parents,
            )
            held_fds.append(child_fd)
            current_path = child_path
            current_fd = child_fd

        yield label, _MutationDirectory(current_path, current_fd)
    except BaselineError:
        raise
    except (ConfigError, OSError, ValueError) as exc:
        raise BaselineError(f"unsafe verification baseline path {path}: {exc}") from exc
    finally:
        for fd in reversed(held_fds):
            try:
                os.close(fd)
            except OSError:
                pass


def _reserve_sibling(
    directory: _MutationDirectory,
    target_name: str,
    suffix: str,
) -> Tuple[int, str]:
    for _attempt in range(100):
        name = f".{target_name}.{secrets.token_hex(8)}{suffix}"
        try:
            return directory.open_exclusive(name), name
        except FileExistsError:
            continue
    raise OSError("cannot allocate a unique baseline sidecar")


def _write_durable_temp(
    directory: _MutationDirectory,
    target_name: str,
    payload: bytes,
    mode: Optional[int] = None,
) -> str:
    fd, temp_name = _reserve_sibling(directory, target_name, ".tmp")
    try:
        if mode is not None and os.name != "nt":
            os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            directory.unlink(temp_name)
        except OSError:
            pass
        raise
    return temp_name


def _read_bounded_sibling_bytes(
    directory: _MutationDirectory,
    name: str,
    label: str,
) -> bytes:
    """Read one plain sibling through the mutation's identity-bound parent."""
    try:
        fd = directory.open_read(name)
    except FileNotFoundError as exc:
        raise ValueError(f"File disappeared while hashing: {label}") from exc
    try:
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or _is_windows_reparse_point(opened):
                raise ValueError(f"Unsupported working-tree file type at {label}")
            if opened.st_size > MAX_BASELINE_BYTES:
                raise GuardrailError(
                    "Hash guardrail exceeded: file too large "
                    f"({opened.st_size} bytes) at {label}"
                )
            output = io.BytesIO()
            total = 0
            while True:
                requested = min(64 * 1024, MAX_BASELINE_BYTES - total + 1)
                chunk = stream.read(requested)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_BASELINE_BYTES:
                    raise GuardrailError(
                        "Hash guardrail exceeded: file too large "
                        f"(>{MAX_BASELINE_BYTES} bytes) at {label}"
                    )
                output.write(chunk)
            finished = os.fstat(stream.fileno())
        current = directory.lstat(name)
        identity_changed = (
            not stat.S_ISREG(current.st_mode)
            or _is_windows_reparse_point(current)
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        )
        content_changed = (
            opened.st_size != finished.st_size
            or opened.st_mtime_ns != finished.st_mtime_ns
            or stat.S_IMODE(opened.st_mode) != stat.S_IMODE(finished.st_mode)
            or finished.st_size != total
            or current.st_size != finished.st_size
            or current.st_mtime_ns != finished.st_mtime_ns
            or stat.S_IMODE(current.st_mode) != stat.S_IMODE(finished.st_mode)
        )
        if identity_changed or content_changed:
            raise ValueError(f"File changed while hashing: {label}")
        return output.getvalue()
    except FileNotFoundError as exc:
        raise ValueError(f"File disappeared while hashing: {label}") from exc


def write_baseline_create_only(
    path: Path,
    text: str,
    *,
    repo_root: Path,
) -> None:
    """Durably publish a complete baseline without ever replacing a target."""
    payload = _baseline_payload_bytes(text)
    with _mutation_directory(path, repo_root, create_parents=True) as mutation:
        label, directory = mutation
        target_name = path.name
        temp_name: Optional[str] = None
        try:
            temp_name = _write_durable_temp(directory, target_name, payload)
            try:
                # A same-directory hard link is an atomic no-replace
                # publication. The directory binding, rather than a rechecked
                # pathname, closes the ancestor-swap gap at this operation.
                directory.link(temp_name, target_name)
            except FileExistsError as exc:
                raise BaselineError(
                    f"verification baseline already exists: {label}; "
                    "use --update-baseline after reviewing current debt"
                ) from exc
            except OSError as exc:
                raise BaselineError(
                    f"cannot safely create verification baseline {label}: {exc}"
                ) from exc
            directory.unlink(temp_name)
            temp_name = None
            directory.fsync()
        except BaselineError:
            raise
        except (OSError, ValueError) as exc:
            raise BaselineError(
                f"cannot safely create verification baseline {label}: {exc}"
            ) from exc
        finally:
            if temp_name is not None:
                try:
                    directory.unlink(temp_name)
                except OSError:
                    pass


def _require_expected_live_bytes(
    directory: _MutationDirectory,
    name: str,
    label: str,
    expected: bytes,
) -> None:
    try:
        current = _read_bounded_sibling_bytes(directory, name, label)
    except (GuardrailError, OSError, ValueError) as exc:
        raise BaselineError(
            f"verification baseline changed before update: {label}; refusing to write"
        ) from exc
    if current != expected:
        raise BaselineError(
            f"verification baseline changed before update: {label}; refusing to write"
        )


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        stat.S_ISREG(right.st_mode)
        and not _is_windows_reparse_point(right)
        and (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)
    )


def _replace_baseline_in_directory(
    directory: _MutationDirectory,
    target_name: str,
    label: str,
    payload: bytes,
    expected: bytes,
) -> None:
    lock_name = f".{target_name}.boundver-update.lock"
    try:
        lock_fd = directory.open_exclusive(lock_name)
    except FileExistsError as exc:
        raise BaselineError(
            f"verification baseline update is already in progress: {label}"
        ) from exc
    except OSError as exc:
        raise BaselineError(
            f"cannot lock verification baseline for update {label}: {exc}"
        ) from exc

    temp_name: Optional[str] = None
    claimed_name: Optional[str] = None
    preserve_claim = False
    # Capture the exclusively created file's identity before any operation
    # that can be interrupted. Cleanup can then remove only this exact lock,
    # never a competing replacement installed during unwinding.
    lock_identity: Optional[os.stat_result] = os.fstat(lock_fd)
    try:
        with os.fdopen(lock_fd, "wb") as handle:
            handle.write(b"boundver verify --update-baseline\n")
            handle.flush()
            os.fsync(handle.fileno())
        _require_expected_live_bytes(directory, target_name, label, expected)
        try:
            existing_mode = stat.S_IMODE(directory.lstat(target_name).st_mode)
        except OSError as exc:
            raise BaselineError(
                f"verification baseline changed before update: {label}; refusing to write"
            ) from exc
        temp_name = _write_durable_temp(
            directory,
            target_name,
            payload,
            existing_mode,
        )

        # Recheck both the selected bytes and our exclusive-lock identity at
        # the last possible point before the atomic claim.
        _require_expected_live_bytes(directory, target_name, label, expected)
        try:
            current_lock = directory.lstat(lock_name)
        except OSError as exc:
            raise BaselineError(
                f"verification baseline update lock changed: {label}; refusing to write"
            ) from exc
        if lock_identity is None or not _same_file_identity(
            lock_identity, current_lock
        ):
            raise BaselineError(
                f"verification baseline update lock changed: {label}; refusing to write"
            )

        # Atomically claim whatever currently occupies the target, then
        # validate the claimed bytes. Every operation stays relative to the
        # validated directory object, so an ancestor pathname swap cannot
        # redirect either the claim, restoration, or publication.
        claim_fd, claimed_name = _reserve_sibling(
            directory,
            target_name,
            ".claim",
        )
        reserved_claim_identity = os.fstat(claim_fd)
        os.close(claim_fd)
        preserve_claim = True
        try:
            directory.replace(target_name, claimed_name)
        except BaseException:
            # An injected interrupt may arrive after the atomic move completed
            # and before Python observes its return. A changed claim identity
            # then owns displaced user bytes and must be restored exclusively.
            try:
                current_claim_identity = directory.lstat(claimed_name)
            except OSError:
                claim_was_replaced = True
            else:
                claim_was_replaced = not _same_file_identity(
                    reserved_claim_identity,
                    current_claim_identity,
                )
            if claim_was_replaced:
                try:
                    directory.link(claimed_name, target_name)
                except (FileExistsError, OSError):
                    pass
                else:
                    directory.unlink(claimed_name)
                    claimed_name = None
                    preserve_claim = False
                    directory.fsync()
            else:
                preserve_claim = False
            raise
        try:
            _require_expected_live_bytes(directory, claimed_name, label, expected)
        except BaseException as exc:
            restore_error: Optional[OSError] = None
            target_occupied = False
            try:
                directory.link(claimed_name, target_name)
            except FileExistsError:
                target_occupied = True
            except OSError as restore_exc:
                restore_error = restore_exc
            else:
                directory.unlink(claimed_name)
                claimed_name = None
                preserve_claim = False
                directory.fsync()
            if isinstance(exc, BaselineError):
                if target_occupied:
                    raise BaselineError(
                        "verification baseline changed during update; the current "
                        "target was preserved and earlier competing bytes remain at "
                        f"{claimed_name}"
                    ) from exc
                if restore_error is not None:
                    raise BaselineError(
                        "verification baseline changed during update and could not "
                        "be restored; recover the competing bytes from "
                        f"{claimed_name}"
                    ) from restore_error
                raise BaselineError(
                    f"verification baseline changed during update: {label}; "
                    "competing bytes were restored and no update was written"
                ) from exc
            raise

        try:
            directory.link(temp_name, target_name)
        except FileExistsError as exc:
            # The claim matched the reviewed bytes, so it is safe to discard;
            # the late writer at the target must remain untouched.
            directory.unlink(claimed_name)
            claimed_name = None
            preserve_claim = False
            directory.fsync()
            raise BaselineError(
                f"verification baseline changed during update: {label}; "
                "the competing target was preserved"
            ) from exc
        except OSError as exc:
            # Publication failed while the target is absent. Restore reviewed
            # bytes exclusively, retaining the claim only when recovery fails.
            try:
                directory.link(claimed_name, target_name)
            except FileExistsError:
                directory.unlink(claimed_name)
                claimed_name = None
                preserve_claim = False
            except OSError as restore_exc:
                preserve_claim = True
                raise BaselineError(
                    "cannot publish or restore verification baseline; recover "
                    f"the reviewed bytes from {claimed_name}"
                ) from restore_exc
            else:
                directory.unlink(claimed_name)
                claimed_name = None
                preserve_claim = False
            directory.fsync()
            raise BaselineError(
                f"cannot safely publish verification baseline update {label}: {exc}"
            ) from exc

        directory.unlink(temp_name)
        temp_name = None
        directory.unlink(claimed_name)
        claimed_name = None
        preserve_claim = False
        directory.fsync()
    except BaselineError:
        raise
    except (OSError, ValueError) as exc:
        raise BaselineError(
            f"cannot safely update verification baseline {label}: {exc}"
        ) from exc
    finally:
        if temp_name is not None:
            try:
                directory.unlink(temp_name)
            except OSError:
                pass
        if claimed_name is not None and not preserve_claim:
            try:
                directory.unlink(claimed_name)
            except OSError:
                pass
        if lock_identity is not None:
            try:
                current_lock = directory.lstat(lock_name)
                if _same_file_identity(lock_identity, current_lock):
                    directory.unlink(lock_name)
                    directory.fsync()
            except OSError:
                pass


def replace_baseline_if_unchanged(
    path: Path,
    text: str,
    expected: bytes,
    *,
    repo_root: Path,
) -> None:
    """Compare and replace a baseline under an exclusive sidecar lock."""
    payload = _baseline_payload_bytes(text)
    if not isinstance(expected, bytes) or len(expected) > MAX_BASELINE_BYTES:
        raise BaselineError("selected verification baseline bytes are invalid")
    with _mutation_directory(path, repo_root, create_parents=False) as mutation:
        label, directory = mutation
        _replace_baseline_in_directory(
            directory,
            path.name,
            label,
            payload,
            expected,
        )


def policy_digest(facet_policy: dict) -> str:
    encoded = _bounded_json_dumps(
        facet_policy,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def lock_digest(lockfile: dict) -> str:
    encoded = _bounded_json_dumps(
        lockfile,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def baseline_context(
    *,
    config: dict,
    lockfile: dict,
    source: str,
    components_filter: List[str],
    facets: Optional[List[str]],
    transitive: bool,
    facet_policy: dict,
) -> dict:
    return {
        "project": config.get("project", "unknown"),
        "lock_schema": lockfile.get("schema", ""),
        "lock_digest": lock_digest(lockfile),
        "config_contract": lockfile.get("config_contract", ""),
        "source": source,
        "components_filter": sorted(set(components_filter)),
        "facets": None if facets is None else sorted(set(facets)),
        "transitive": transitive,
        "policy_digest": policy_digest(facet_policy),
    }


def create_baseline(context: dict, issues: List[str]) -> dict:
    identities: Dict[str, dict] = {}
    current_compat_subjects = _compat_subjects(issues)
    for issue, identity in _issues_with_identities(issues):
        if identity is None:
            ancillary_subject = _ancillary_compat_subject(issue)
            if ancillary_subject in current_compat_subjects:
                # Version and parsed-semver metadata are two representations of
                # the same observed version change that produced this gated
                # compat mismatch. They are covered by that compat identity,
                # never stored as independently baselinable metadata.
                continue
            raise BaselineError(
                "cannot baseline integrity, configuration, or unclassified issue: "
                + issue[:MAX_BASELINE_TEXT]
            )
        identities[identity["id"]] = identity
    if len(identities) > MAX_BASELINE_VIOLATIONS:
        raise BaselineError(
            f"verification has more than {MAX_BASELINE_VIOLATIONS} distinct violations"
        )
    payload = {
        "$schema": BASELINE_SCHEMA_URL,
        "schema": BASELINE_SCHEMA,
        **context,
        "violations": sorted(identities.values(), key=lambda item: item["id"]),
    }
    return validate_baseline(payload)


def apply_baseline(
    baseline: dict,
    context: dict,
    issues: List[str],
) -> Tuple[List[str], List[str], List[str]]:
    """Return ``(new, acknowledged, stale_ids)`` for current issues."""
    for field, current in context.items():
        if baseline.get(field) != current:
            raise BaselineError(
                f"verification baseline {field} does not match this invocation; "
                "review the changed scope and explicitly update the baseline"
            )
    known = {entry["id"] for entry in baseline["violations"]}
    known_compat_subjects = {
        entry["subject"]
        for entry in baseline["violations"]
        if entry["kind"] == "component-facet" and entry["facet"] == "compat"
    }
    current_compat_subjects = _compat_subjects(issues)
    observed = set()
    new: List[str] = []
    acknowledged: List[str] = []
    for issue, identity in _issues_with_identities(issues):
        if identity is not None:
            observed.add(identity["id"])
        if identity is not None and identity["id"] in known:
            acknowledged.append(issue)
        elif (
            _ancillary_compat_subject(issue) in known_compat_subjects
            and _ancillary_compat_subject(issue) in current_compat_subjects
        ):
            acknowledged.append(issue)
        else:
            new.append(issue)
    stale = sorted(known - observed)
    return new, acknowledged, stale


def baseline_change_ids(old: Optional[dict], new: dict) -> Tuple[List[str], List[str]]:
    old_ids = set() if old is None else {entry["id"] for entry in old["violations"]}
    new_ids = {entry["id"] for entry in new["violations"]}
    return sorted(new_ids - old_ids), sorted(old_ids - new_ids)


def dump_baseline(value: dict) -> str:
    # Build at most the same byte budget accepted by ``load_baseline``. Using
    # the public incremental encoder avoids first allocating an arbitrarily
    # large string for a baseline this release would then be unable to read.
    encoder = json.JSONEncoder(
        ensure_ascii=True,
        allow_nan=False,
        indent=2,
        sort_keys=False,
    )
    chunks = []
    encoded_size = 1  # trailing newline
    for chunk in encoder.iterencode(value):
        encoded_size += len(chunk.encode("utf-8"))
        if encoded_size > MAX_BASELINE_BYTES:
            raise BaselineError(
                "verification baseline exceeds the "
                f"{MAX_BASELINE_BYTES}-byte storage limit"
            )
        chunks.append(chunk)
    return "".join(chunks) + "\n"
