#!/usr/bin/env python3
"""Fail closed when versioned public release surfaces disagree.

This check covers repository content that the documentation site, package
indexes, GitHub Releases, GitHub Marketplace, GHCR, Homebrew, and GitLab
distribution surfaces derive from the release commit. Remote publication
state is verified separately after each service accepts the immutable
artifacts.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import stat
import sys
from pathlib import Path
from typing import Iterable, Optional, Sequence

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.9/3.10 release tooling
    import tomli as tomllib


def _load_release_note_extractor():
    path = Path(__file__).resolve().with_name("release_changelog.py")
    spec = importlib.util.spec_from_file_location(
        "_boundver_release_changelog", path
    )
    if spec is None or spec.loader is None:  # pragma: no cover - import invariant
        raise RuntimeError(f"cannot load release changelog helper: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.extract_release_notes


extract_release_notes = _load_release_note_extractor()


TAG_RE = re.compile(
    r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
)
RAW_SCHEMA_RE = re.compile(
    r"https://raw\.githubusercontent\.com/yzm1/boundver/"
    r"(?P<ref>[^/\s\"'<>]+)/(?P<path>[^\s\"'<>]*schema\.json)"
)
TEXT_SUFFIXES = {".json", ".md", ".py", ".toml", ".yaml", ".yml"}
SKIP_PARTS = {
    ".git",
    ".pytest_cache",
    ".mypy_cache",
    ".tox",
    ".venv",
    "build",
    "dist",
    "__pycache__",
}
EXCLUDED_RELEASE_PATHS = frozenset({"docs/PROJECT_REVIEW.md"})
RELEASE_DOCS = (
    "README.md",
    "docs/index.md",
    "docs/demo.md",
    "docs/getting-started.md",
    "docs/ci-cookbook.md",
    "docs/comparison.md",
    "docs/distribution.md",
    "docs/WHY_BOUNDVER.md",
)

MAX_RELEASE_TREE_ENTRIES = 50_000
MAX_RELEASE_FILE_COUNT = 20_000
MAX_RELEASE_PATH_BYTES = 16 * 1024
MAX_RELEASE_TOTAL_PATH_BYTES = 16 * 1024 * 1024
MAX_RELEASE_FILE_BYTES = 16 * 1024 * 1024
MAX_RELEASE_TOTAL_BYTES = 256 * 1024 * 1024
MAX_READINESS_DIAGNOSTICS = 512
MAX_READINESS_DIAGNOSTIC_CHARS = 4 * 1024
MAX_READINESS_DIAGNOSTIC_TOTAL_CHARS = 256 * 1024
MAX_JSON_INTEGER_DIGITS = 4_300
MAX_JSON_NUMBER_CHARS = MAX_JSON_INTEGER_DIGITS + 32
MAX_TOML_INTEGER_DIGITS = 640
_READ_CHUNK_BYTES = 64 * 1024


class ReleaseReadError(ValueError):
    """A release candidate cannot be inspected safely within fixed limits."""


def _bounded_json_int(value: str) -> int:
    if re.fullmatch(r"-?(?:0|[1-9][0-9]*)", value) is None:
        raise ValueError("invalid JSON integer")
    negative = value.startswith("-")
    digits = value[1:] if negative else value
    if len(digits) > MAX_JSON_INTEGER_DIGITS:
        raise ValueError(
            "JSON integer exceeds the "
            f"{MAX_JSON_INTEGER_DIGITS}-decimal-digit limit"
        )
    result = 0
    first = len(digits) % 9 or 9
    for end in range(first, len(digits) + 1, 9):
        width = first if end == first else 9
        result = result * (10**width) + int(digits[end - width : end])
    return -result if negative else result


def _bounded_json_float(value: str) -> float:
    if len(value) > MAX_JSON_NUMBER_CHARS:
        raise ValueError(
            f"JSON number exceeds the {MAX_JSON_NUMBER_CHARS}-character limit"
        )
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite JSON number is not supported")
    return parsed


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> object:
    raise ValueError("non-finite JSON number is not supported")


def _load_json(text: str) -> object:
    return json.loads(
        text,
        object_pairs_hook=_unique_json_object,
        parse_constant=_reject_json_constant,
        parse_float=_bounded_json_float,
        parse_int=_bounded_json_int,
    )


def _bounded_toml_float(value: str) -> float:
    if len(value) > MAX_JSON_NUMBER_CHARS:
        raise ValueError(
            f"TOML number exceeds the {MAX_JSON_NUMBER_CHARS}-character limit"
        )
    parsed = float(value.replace("_", ""))
    if not math.isfinite(parsed):
        raise ValueError("non-finite TOML number is not supported")
    return parsed


def _toml_has_oversized_numeric_token(text: str) -> bool:
    """Detect oversized TOML numeric values outside strings/comments."""
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

    def in_key_context() -> bool:
        if table_header_depth:
            return True
        if containers:
            return containers[-1] == inline_key_frame
        return not root_in_value

    def reset_line() -> None:
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
        if text.startswith('"""', index):
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


def _is_windows_reparse_point(identity: os.stat_result) -> bool:
    attributes = getattr(identity, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _identity_changed(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        or before.st_mode != after.st_mode
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    )


def _path_bytes(path: Path) -> int:
    return len(path.as_posix().encode("utf-8", "surrogatepass"))


def _read_bytes(path: Path, *, max_bytes: Optional[int] = None) -> bytes:
    """Read one stable regular file with a hard ceiling and sentinel byte."""
    limit = MAX_RELEASE_FILE_BYTES if max_bytes is None else max_bytes
    if limit < 0:
        raise ValueError("release file byte limit must be non-negative")
    try:
        initial = path.lstat()
        if not stat.S_ISREG(initial.st_mode) or _is_windows_reparse_point(initial):
            raise ReleaseReadError(f"release path is not a regular file: {path}")
        if initial.st_size > limit:
            raise ReleaseReadError(
                f"release file exceeds the {limit}-byte limit: {path}"
            )
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or _identity_changed(initial, opened)
            ):
                raise ReleaseReadError(
                    f"release file changed while opening: {path}"
                )
            content = bytearray()
            while True:
                remaining = limit - len(content)
                chunk = stream.read(min(_READ_CHUNK_BYTES, remaining + 1))
                if not chunk:
                    break
                if len(chunk) > remaining:
                    raise ReleaseReadError(
                        f"release file exceeds the {limit}-byte limit: {path}"
                    )
                content.extend(chunk)
            finished = os.fstat(stream.fileno())
        current = path.lstat()
    except FileNotFoundError as exc:
        raise ReleaseReadError(f"release file disappeared while reading: {path}") from exc
    if (
        not stat.S_ISREG(current.st_mode)
        or _is_windows_reparse_point(current)
        or _identity_changed(opened, finished)
        or _identity_changed(finished, current)
        or finished.st_size != len(content)
    ):
        raise ReleaseReadError(f"release file changed while reading: {path}")
    return bytes(content)


def _read_text(path: Path, *, max_bytes: Optional[int] = None) -> str:
    return _read_bytes(path, max_bytes=max_bytes).decode("utf-8")


def _capture_ancestors(repo: Path, path: Path) -> list[tuple[Path, os.stat_result]]:
    try:
        relative = path.relative_to(repo)
    except ValueError as exc:
        raise ReleaseReadError(f"release path escapes repository: {path}") from exc
    ancestors: list[tuple[Path, os.stat_result]] = []
    current = repo
    for part in relative.parts[:-1]:
        if part in {"", ".", ".."}:
            raise ReleaseReadError(f"release path escapes repository: {path}")
        current = current / part
        identity = current.lstat()
        if not stat.S_ISDIR(identity.st_mode) or _is_windows_reparse_point(identity):
            raise ReleaseReadError(
                f"release path has an unsafe directory ancestor: {path}"
            )
        ancestors.append((current, identity))
    return ancestors


def _verify_ancestors(
    ancestors: list[tuple[Path, os.stat_result]], path: Path
) -> None:
    for ancestor, before in ancestors:
        try:
            after = ancestor.lstat()
        except FileNotFoundError as exc:
            raise ReleaseReadError(
                f"release path changed while reading: {path}"
            ) from exc
        if (
            not stat.S_ISDIR(after.st_mode)
            or _is_windows_reparse_point(after)
            or _identity_changed(before, after)
        ):
            raise ReleaseReadError(f"release path changed while reading: {path}")


class _ReadBudget:
    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.total = 0

    def read_bytes(self, path: Path) -> bytes:
        remaining = MAX_RELEASE_TOTAL_BYTES - self.total
        if remaining <= 0:
            raise ReleaseReadError(
                "release surface reads exceed the "
                f"{MAX_RELEASE_TOTAL_BYTES}-byte aggregate limit"
            )
        ancestors = _capture_ancestors(self.repo, path)
        effective_limit = min(MAX_RELEASE_FILE_BYTES, remaining)
        try:
            content = _read_bytes(path, max_bytes=effective_limit)
        except (OSError, ReleaseReadError) as exc:
            # A failed or unstable stream may already have consumed bytes that
            # cannot be measured reliably.  Exhaust the shared budget so a
            # hostile candidate cannot trigger another large read afterward.
            self.total = MAX_RELEASE_TOTAL_BYTES
            if remaining < MAX_RELEASE_FILE_BYTES and "exceeds" in str(exc):
                raise ReleaseReadError(
                    "release surface reads exceed the "
                    f"{MAX_RELEASE_TOTAL_BYTES}-byte aggregate limit"
                ) from exc
            raise
        self.total += len(content)
        _verify_ancestors(ancestors, path)
        return content

    def read_text(self, path: Path) -> str:
        return self.read_bytes(path).decode("utf-8")


class _Diagnostics(list[str]):
    """Retain a bounded prefix of diagnostics and one truncation marker."""

    _MARKER = "additional release-readiness diagnostics were omitted"

    def __init__(self) -> None:
        super().__init__()
        self._total_chars = 0
        self._truncated = False

    def _mark_truncated(self) -> None:
        if self._truncated:
            return
        self._truncated = True
        count_limit = max(1, MAX_READINESS_DIAGNOSTICS)
        total_limit = max(1, MAX_READINESS_DIAGNOSTIC_TOTAL_CHARS)
        marker = self._MARKER[:total_limit]
        while self and (
            len(self) >= count_limit
            or self._total_chars + len(marker) > total_limit
        ):
            removed = super().pop()
            self._total_chars -= len(removed)
        if len(self) < count_limit and self._total_chars + len(marker) <= total_limit:
            super().append(marker)
            self._total_chars += len(marker)

    def append(self, message: str) -> None:
        if self._truncated:
            return
        per_item_limit = max(1, MAX_READINESS_DIAGNOSTIC_CHARS)
        normalized = str(message)
        if len(normalized) > per_item_limit:
            suffix = "... [truncated]"
            if per_item_limit <= len(suffix):
                normalized = suffix[:per_item_limit]
            else:
                normalized = normalized[: per_item_limit - len(suffix)] + suffix
        reserved_total = max(0, MAX_READINESS_DIAGNOSTIC_TOTAL_CHARS - len(self._MARKER))
        reserved_count = max(0, MAX_READINESS_DIAGNOSTICS - 1)
        if (
            len(self) >= reserved_count
            or self._total_chars + len(normalized) > reserved_total
        ):
            self._mark_truncated()
            return
        super().append(normalized)
        self._total_chars += len(normalized)


def _release_files(repo: Path) -> Iterable[Path]:
    """Yield a deterministic, bounded release-surface inventory."""
    pending: list[tuple[Path, Path]] = [(repo, Path())]
    selected: list[Path] = []
    entry_count = 0
    total_path_bytes = 0
    while pending:
        directory, relative_directory = pending.pop()
        before = directory.lstat()
        if not stat.S_ISDIR(before.st_mode) or _is_windows_reparse_point(before):
            raise ReleaseReadError(f"release tree directory is unsafe: {directory}")
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    entry_count += 1
                    if entry_count > MAX_RELEASE_TREE_ENTRIES:
                        raise ReleaseReadError(
                            "release tree exceeds the "
                            f"{MAX_RELEASE_TREE_ENTRIES}-entry traversal limit"
                        )
                    relative = relative_directory / entry.name
                    encoded_length = _path_bytes(relative)
                    if encoded_length > MAX_RELEASE_PATH_BYTES:
                        raise ReleaseReadError(
                            "release path exceeds the "
                            f"{MAX_RELEASE_PATH_BYTES}-byte limit"
                        )
                    total_path_bytes += encoded_length
                    if total_path_bytes > MAX_RELEASE_TOTAL_PATH_BYTES:
                        raise ReleaseReadError(
                            "release paths exceed the "
                            f"{MAX_RELEASE_TOTAL_PATH_BYTES}-byte aggregate limit"
                        )
                    if (
                        relative.as_posix() in EXCLUDED_RELEASE_PATHS
                        or any(
                            part in SKIP_PARTS or part.endswith(".egg-info")
                            for part in relative.parts
                        )
                        or (relative.parts and relative.parts[0] == "tests")
                    ):
                        continue
                    entry_path = Path(entry.path)
                    identity = entry_path.lstat()
                    if stat.S_ISDIR(identity.st_mode):
                        if _is_windows_reparse_point(identity):
                            raise ReleaseReadError(
                                f"release tree contains a reparse directory: {relative}"
                            )
                        pending.append((entry_path, relative))
                        continue
                    if _is_windows_reparse_point(identity):
                        raise ReleaseReadError(
                            f"release tree contains a reparse path: {relative}"
                        )
                    if not stat.S_ISREG(identity.st_mode):
                        raise ReleaseReadError(
                            f"release tree contains a non-regular path: {relative}"
                        )
                    if relative.suffix.lower() not in TEXT_SUFFIXES:
                        continue
                    if len(selected) >= MAX_RELEASE_FILE_COUNT:
                        raise ReleaseReadError(
                            "release surfaces exceed the "
                            f"{MAX_RELEASE_FILE_COUNT}-file limit"
                        )
                    selected.append(entry_path)
        except FileNotFoundError as exc:
            raise ReleaseReadError(
                f"release tree changed while traversing: {directory}"
            ) from exc
        after = directory.lstat()
        if (
            not stat.S_ISDIR(after.st_mode)
            or _is_windows_reparse_point(after)
            or _identity_changed(before, after)
        ):
            raise ReleaseReadError(
                f"release tree changed while traversing: {directory}"
            )
    yield from sorted(selected, key=lambda path: path.relative_to(repo).as_posix())


def _json(path: Path, errors: list[str], reads: _ReadBudget) -> object | None:
    try:
        return _load_json(reads.read_text(path))
    except (
        OSError,
        UnicodeError,
        ReleaseReadError,
        ValueError,
        RecursionError,
        OverflowError,
    ) as exc:
        errors.append(f"{path}: cannot parse JSON: {exc}")
        return None


def readiness_errors(repo: Path, tag: str) -> list[str]:
    """Return every local release-surface disagreement for ``tag``."""
    repo = repo.resolve()
    errors: list[str] = _Diagnostics()
    if TAG_RE.fullmatch(tag) is None:
        errors.append(f"invalid release tag: {tag!r}")
        return errors
    version = tag.removeprefix("v")
    reads = _ReadBudget(repo)

    try:
        project_text = reads.read_text(repo / "pyproject.toml")
        if _toml_has_oversized_numeric_token(project_text):
            raise ReleaseReadError(
                "pyproject.toml contains a numeric token exceeding the "
                f"{MAX_TOML_INTEGER_DIGITS}-digit cross-runtime limit"
            )
        project = tomllib.loads(
            project_text, parse_float=_bounded_toml_float
        )["project"]
    except (
        OSError,
        UnicodeError,
        ReleaseReadError,
        KeyError,
        TypeError,
        ValueError,
        tomllib.TOMLDecodeError,
        RecursionError,
        OverflowError,
    ) as exc:
        errors.append(f"pyproject.toml cannot be read as PEP 621 metadata: {exc}")
        return errors
    if project.get("name") != "boundver":
        errors.append("pyproject.toml project.name must be 'boundver'")
    if project.get("version") != version:
        errors.append(
            f"pyproject.toml version {project.get('version')!r} does not match {tag}"
        )
    required_urls = {
        "Homepage": "https://github.com/yzm1/boundver",
        "Documentation": "https://yzm1.github.io/boundver/",
        "Changelog": "https://github.com/yzm1/boundver/blob/main/CHANGELOG.md",
        "Issues": "https://github.com/yzm1/boundver/issues",
        "Repository": "https://github.com/yzm1/boundver",
        "GitHub Action": "https://github.com/marketplace/actions/boundver",
        "Container": "https://github.com/yzm1/boundver/pkgs/container/boundver",
        "Homebrew": "https://github.com/yzm1/homebrew-boundver",
    }
    urls = project.get("urls")
    if not isinstance(urls, dict):
        errors.append("pyproject.toml project.urls must be a table")
    else:
        for label, expected in required_urls.items():
            if urls.get(label) != expected:
                errors.append(
                    f"pyproject.toml project.urls.{label} must be {expected!r}"
                )

    try:
        extract_release_notes(
            reads.read_text(repo / "CHANGELOG.md"), tag, mode="pre-tag"
        )
    except (OSError, UnicodeError, ReleaseReadError, ValueError) as exc:
        errors.append(str(exc))

    for relative in RELEASE_DOCS:
        path = repo / relative
        try:
            text = reads.read_text(path)
        except (OSError, UnicodeError, ReleaseReadError) as exc:
            errors.append(f"{relative}: cannot read release documentation: {exc}")
            continue
        if re.search(r"\bunreleased\b", text, flags=re.IGNORECASE):
            errors.append(f"{relative}: still describes the release as unreleased")
        if re.search(
            rf"(?:after|once|when)[^\n]{{0,80}}{re.escape(version)}[^\n]{{0,80}}published",
            text,
            flags=re.IGNORECASE,
        ):
            errors.append(f"{relative}: still contains pre-publication instructions")
        if "corrective work in progress, not a completed release" in text.lower():
            errors.append(f"{relative}: still marks the corrective release incomplete")

    try:
        readme = reads.read_text(repo / "README.md")
    except (OSError, UnicodeError, ReleaseReadError):
        readme = ""
    for required in (
        f"yzm1/boundver@{tag}",
        "https://pypi.org/project/boundver/",
        "https://github.com/marketplace/actions/boundver",
        "https://yzm1.github.io/boundver/",
        "ghcr.io/yzm1/boundver:",
        "brew install yzm1/boundver/boundver",
        "https://yzm1.github.io/boundver/assets/verify-demo.svg",
        "docs/RELEASING.md",
    ):
        if required not in readme:
            errors.append(f"README.md must contain release-facing reference {required!r}")

    try:
        release_files = list(_release_files(repo))
    except (OSError, ReleaseReadError) as exc:
        errors.append(f"cannot inventory release surfaces: {exc}")
        release_files = []

    for path in release_files:
        relative = path.relative_to(repo)
        try:
            text = reads.read_text(path)
        except (OSError, UnicodeError, ReleaseReadError) as exc:
            errors.append(f"{relative}: cannot read release surface: {exc}")
            continue
        for match in RAW_SCHEMA_RE.finditer(text):
            if match.group("ref") != tag:
                errors.append(
                    f"{relative}: schema URL uses {match.group('ref')!r}, expected {tag!r}"
                )

    expected_config_schema = (
        f"https://raw.githubusercontent.com/yzm1/boundver/{tag}/"
        "boundary.config.schema.json"
    )
    expected_lock_schema = (
        f"https://raw.githubusercontent.com/yzm1/boundver/{tag}/"
        "spec/boundary.lock.schema.json"
    )
    config_schema_path = repo / "boundary.config.schema.json"
    packaged_schema_path = repo / "src/boundver/boundary.config.schema.json"
    config_schema = _json(config_schema_path, errors, reads)
    _json(packaged_schema_path, errors, reads)
    try:
        schemas_match = (
            reads.read_bytes(config_schema_path)
            == reads.read_bytes(packaged_schema_path)
        )
    except (OSError, ReleaseReadError) as exc:
        errors.append(f"cannot compare root and packaged config schemas: {exc}")
    else:
        if not schemas_match:
            errors.append("root and packaged config schemas must be byte-identical")
    if isinstance(config_schema, dict) and config_schema.get("$id") != expected_config_schema:
        errors.append(
            f"boundary.config.schema.json $id must be {expected_config_schema!r}"
        )

    lock_schema = _json(repo / "spec/boundary.lock.schema.json", errors, reads)
    if isinstance(lock_schema, dict) and lock_schema.get("$id") != expected_lock_schema:
        errors.append(
            f"spec/boundary.lock.schema.json $id must be {expected_lock_schema!r}"
        )
    expected_lock_fields: dict[str, str] = {}
    if isinstance(lock_schema, dict):
        properties = lock_schema.get("properties")
        if not isinstance(properties, dict):
            errors.append(
                "spec/boundary.lock.schema.json properties must be an object"
            )
        else:
            for field in ("schema", "config_contract"):
                field_schema = properties.get(field)
                expected = (
                    field_schema.get("const")
                    if isinstance(field_schema, dict)
                    else None
                )
                if not isinstance(expected, str) or not expected:
                    errors.append(
                        "spec/boundary.lock.schema.json must define a non-empty "
                        f"const for {field!r}"
                    )
                else:
                    expected_lock_fields[field] = expected

    cli_schema_paths = [
        path
        for path in release_files
        if path.parent == repo / "spec"
        and path.name.startswith("cli-output.")
        and path.name.endswith(".schema.json")
    ]
    for path in cli_schema_paths:
        value = _json(path, errors, reads)
        expected = (
            f"https://raw.githubusercontent.com/yzm1/boundver/{tag}/spec/{path.name}"
        )
        if isinstance(value, dict) and value.get("$id") != expected:
            errors.append(f"{path.relative_to(repo)} $id must be {expected!r}")

    release_paths = {
        path.relative_to(repo).as_posix(): path for path in release_files
    }
    config_paths = [
        path
        for relative, path in release_paths.items()
        if relative == "boundary.config.json"
        or (
            relative.startswith("examples/")
            and len(Path(relative).parts) == 3
            and Path(relative).name == "boundary.config.json"
        )
    ]
    for path in config_paths:
        value = _json(path, errors, reads)
        if isinstance(value, dict) and value.get("$schema") != expected_config_schema:
            errors.append(
                f"{path.relative_to(repo)} $schema must be {expected_config_schema!r}"
            )
    lock_paths = [
        path
        for relative, path in release_paths.items()
        if relative == "boundary.lock.json"
        or (
            relative.startswith("examples/")
            and len(Path(relative).parts) == 3
            and Path(relative).name == "expected.boundary.lock.json"
        )
    ]
    for path in lock_paths:
        value = _json(path, errors, reads)
        if isinstance(value, dict):
            if value.get("$schema") != expected_lock_schema:
                errors.append(
                    f"{path.relative_to(repo)} $schema must be "
                    f"{expected_lock_schema!r}"
                )
            for field, expected in expected_lock_fields.items():
                if value.get(field) != expected:
                    errors.append(
                        f"{path.relative_to(repo)} {field} must be {expected!r}"
                    )

    required_source_urls = {
        "src/boundver/core.py": expected_config_schema,
        "src/boundver/_lockfile.py": expected_lock_schema,
    }
    for relative, expected in required_source_urls.items():
        try:
            text = reads.read_text(repo / relative)
        except (OSError, UnicodeError, ReleaseReadError) as exc:
            errors.append(f"{relative}: cannot verify generated schema URL: {exc}")
            continue
        if expected not in text:
            errors.append(f"{relative}: must generate release-pinned schema URL {expected!r}")

    return errors


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate versioned documentation and package release surfaces."
    )
    parser.add_argument("--tag", required=True, help="Exact vMAJOR.MINOR.PATCH tag")
    parser.add_argument("--repo", type=Path, default=Path("."))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    errors = readiness_errors(args.repo, args.tag)
    if errors:
        print(
            f"Release surface validation failed ({len(errors)} issue(s)):",
            file=sys.stderr,
        )
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print(f"Release surfaces are ready for {args.tag}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
