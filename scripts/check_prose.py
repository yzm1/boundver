#!/usr/bin/env python3
"""Report high-confidence prose smells without turning style into a release gate."""

from __future__ import annotations

import argparse
from bisect import bisect_right
import json
import os
import re
import stat
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
MAX_FILES = 512
MAX_DISCOVERY_ENTRIES = 10_000
MAX_DISCOVERY_DEPTH = 64
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_FINDINGS = 4096
MAX_DISPLAY_PATH_BYTES = 1024
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_ERROR_BYTES = 4096
DEFAULT_SENTENCE_WORDS = 35
WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*")
IMAGE_RE = re.compile(r"!\[[^]]*\]\([^)]*\)")
LINK_RE = re.compile(r"!?(?:\[([^]]*)\])\([^)]*\)")
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
SENTENCE_END_RE = re.compile(r"(?<=[.!?])(?:[\"')\]]*)\s+")
LIST_MARKER_RE = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+")
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
TABLE_RULE_RE = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*$")
DISCOURAGED = (
    (re.compile(r"\bin order to\b", re.IGNORECASE), "Use 'to' unless the distinction matters."),
    (
        re.compile(r"\bit is important to note that?\b", re.IGNORECASE),
        "State the fact directly.",
    ),
    (
        re.compile(r"\bit should be noted that?\b", re.IGNORECASE),
        "State the fact directly.",
    ),
    (
        re.compile(r"\bdue to the fact that\b", re.IGNORECASE),
        "Use 'because'.",
    ),
    (
        re.compile(r"\bat this point in time\b", re.IGNORECASE),
        "Use 'now'.",
    ),
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    message: str
    excerpt: str


@dataclass(frozen=True)
class _Paragraph:
    text: str
    line_starts: tuple[int, ...]
    source_lines: tuple[int, ...]

    def source_line(self, offset: int) -> int:
        index = bisect_right(self.line_starts, offset) - 1
        return self.source_lines[max(index, 0)]


def _file_identity(file_stat: os.stat_result) -> tuple[int, int]:
    return file_stat.st_dev, file_stat.st_ino


def _file_snapshot(
    file_stat: os.stat_result,
) -> tuple[int, int, int, int, int]:
    # Windows path and CRT descriptor stats expose different st_ctime_ns
    # semantics on current Python releases.  st_birthtime_ns is the stable
    # creation field on both; concurrent mutation after opening is prevented
    # by the non-sharing Windows handle below.  POSIX keeps ctime as its
    # unforgeable change signal.
    metadata_time_ns = (
        getattr(file_stat, "st_birthtime_ns", file_stat.st_ctime_ns)
        if os.name == "nt"
        else file_stat.st_ctime_ns
    )
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        metadata_time_ns,
    )


def _is_plain_file(file_stat: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(file_stat, "st_file_attributes", 0)
    return stat.S_ISREG(file_stat.st_mode) and not (attributes & reparse_flag)


def _is_plain_directory(directory_stat: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(directory_stat, "st_file_attributes", 0)
    return stat.S_ISDIR(directory_stat.st_mode) and not (
        attributes & reparse_flag
    )


def _open_windows_directory_descriptor(path: Path) -> int:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    file_share_read = 0x00000001
    open_existing = 3
    file_flag_open_reparse_point = 0x00200000
    file_flag_backup_semantics = 0x02000000
    handle = create_file(
        str(path),
        0,
        file_share_read,
        None,
        open_existing,
        file_flag_open_reparse_point | file_flag_backup_semantics,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        error_code = ctypes.get_last_error()
        raise OSError(
            error_code,
            "cannot open documentation directory without following reparse points",
        )
    try:
        descriptor = msvcrt.open_osfhandle(
            handle,
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
    except BaseException:
        close_handle(handle)
        raise
    try:
        if not _is_plain_directory(os.fstat(descriptor)):
            raise ValueError(f"{path}: is not a plain directory")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_plain_directory_descriptor(
    path: Path,
    expected_stat: os.stat_result | None,
    *,
    parent_descriptor: int | None = None,
    child_name: str | None = None,
) -> int:
    before_path = path.lstat()
    if not _is_plain_directory(before_path):
        raise ValueError(f"{path}: is not a plain directory")
    if (
        expected_stat is not None
        and os.name != "nt"
        and _file_snapshot(before_path) != _file_snapshot(expected_stat)
    ):
        raise ValueError(f"{path}: changed before traversal")

    descriptor = _open_directory_descriptor_no_follow(
        path,
        parent_descriptor=parent_descriptor,
        child_name=child_name,
    )
    try:
        opened_stat = os.fstat(descriptor)
        if not _is_plain_directory(opened_stat):
            raise ValueError(f"{path}: is not a plain directory")
        if _file_snapshot(opened_stat) != _file_snapshot(before_path):
            raise ValueError(f"{path}: changed while opening for traversal")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_directory_descriptor_no_follow(
    path: Path,
    *,
    parent_descriptor: int | None,
    child_name: str | None,
) -> int:
    if os.name == "nt":
        return _open_windows_directory_descriptor(path)
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    if parent_descriptor is None:
        return os.open(path, flags)
    if child_name is None:
        raise ValueError("child directory name is required")
    return os.open(
        child_name,
        flags,
        dir_fd=parent_descriptor,
    )


def _open_read_descriptor(path: Path) -> int:
    if os.name != "nt":
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        return os.open(path, flags)

    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    generic_read = 0x80000000
    file_share_read = 0x00000001
    open_existing = 3
    file_attribute_normal = 0x00000080
    file_flag_open_reparse_point = 0x00200000
    handle = create_file(
        str(path),
        generic_read,
        file_share_read,
        None,
        open_existing,
        file_attribute_normal | file_flag_open_reparse_point,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        error_code = ctypes.get_last_error()
        raise OSError(
            error_code,
            "cannot open prose input while excluding concurrent writers",
        )
    try:
        return msvcrt.open_osfhandle(
            handle,
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
    except BaseException:
        close_handle(handle)
        raise


def _read_bounded(
    path: Path,
    display_path: str,
    expected_stat: os.stat_result,
) -> str:
    before_path = path.lstat()
    if not _is_plain_file(before_path):
        raise ValueError(f"{display_path}: is not a regular file")
    if _file_snapshot(before_path) != _file_snapshot(expected_stat):
        raise ValueError(f"{display_path}: changed before reading")
    if before_path.st_size > MAX_FILE_BYTES:
        raise ValueError(f"{display_path}: exceeds {MAX_FILE_BYTES} bytes")

    descriptor = _open_read_descriptor(path)
    try:
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            before_descriptor = os.fstat(stream.fileno())
            if not _is_plain_file(before_descriptor):
                raise ValueError(f"{display_path}: is not a regular file")
            if _file_snapshot(before_descriptor) != _file_snapshot(expected_stat):
                raise ValueError(f"{display_path}: changed while opening")
            payload = stream.read(MAX_FILE_BYTES + 1)
            after_descriptor = os.fstat(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if len(payload) > MAX_FILE_BYTES:
        raise ValueError(f"{display_path}: exceeds {MAX_FILE_BYTES} bytes")

    after_path = path.lstat()
    if not _is_plain_file(after_path):
        raise ValueError(f"{display_path}: changed while reading")
    if (
        _file_snapshot(before_descriptor) != _file_snapshot(after_descriptor)
        or _file_identity(after_descriptor) != _file_identity(after_path)
        or after_descriptor.st_size != after_path.st_size
        or after_descriptor.st_mtime_ns != after_path.st_mtime_ns
        or after_descriptor.st_size != len(payload)
    ):
        raise ValueError(f"{display_path}: changed while reading")
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{display_path}: is not valid UTF-8") from exc


def _clean_markdown(line: str) -> str:
    text = IMAGE_RE.sub("", line)
    text = LINK_RE.sub(lambda match: match.group(1) or "", text)
    text = INLINE_CODE_RE.sub(" code ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s+", "", text)
    text = LIST_MARKER_RE.sub("", text)
    text = text.replace("**", "").replace("__", "").replace("~~", "")
    return " ".join(text.split())


def _paragraphs(text: str) -> Iterator[_Paragraph]:
    lines = text.splitlines()
    front_matter = bool(lines and lines[0].strip() == "---")
    in_fence = False
    fence_character = ""
    pending: list[tuple[int, str]] = []

    def flush() -> _Paragraph | None:
        nonlocal pending
        if not pending:
            return None
        pieces: list[str] = []
        line_starts: list[int] = []
        source_lines: list[int] = []
        offset = 0
        for source_line, cleaned_line in pending:
            if pieces:
                offset += 1
            line_starts.append(offset)
            source_lines.append(source_line)
            pieces.append(cleaned_line)
            offset += len(cleaned_line)
        pending = []
        return _Paragraph(
            text=" ".join(pieces),
            line_starts=tuple(line_starts),
            source_lines=tuple(source_lines),
        )

    for line_number, raw_line in enumerate(lines, start=1):
        stripped = raw_line.strip()
        if front_matter:
            if line_number > 1 and stripped == "---":
                front_matter = False
            continue

        fence = FENCE_RE.match(raw_line)
        if fence:
            marker = fence.group(1)
            if not in_fence:
                result = flush()
                if result:
                    yield result
                in_fence = True
                fence_character = marker[0]
            elif marker[0] == fence_character:
                in_fence = False
            continue
        if in_fence:
            continue

        skip = (
            not stripped
            or raw_line.startswith("    ")
            or stripped.startswith("<")
            or stripped.startswith("|")
            or TABLE_RULE_RE.match(raw_line) is not None
        )
        starts_list_item = LIST_MARKER_RE.match(raw_line) is not None
        if skip or starts_list_item:
            result = flush()
            if result:
                yield result
            if skip:
                continue

        cleaned = _clean_markdown(raw_line)
        if not cleaned:
            continue
        pending.append((line_number, cleaned))
        if starts_list_item:
            result = flush()
            if result:
                yield result

    result = flush()
    if result:
        yield result


def _sentence_spans(text: str) -> Iterator[tuple[int, str]]:
    start = 0
    for boundary in SENTENCE_END_RE.finditer(text):
        if boundary.start() > start:
            yield start, text[start : boundary.start()]
        start = boundary.end()
    if start < len(text):
        yield start, text[start:]


def _excerpt(text: str, limit: int = 180) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _retain_finding(
    findings: list[Finding],
    finding: Finding,
    *,
    max_findings: int,
) -> None:
    if len(findings) >= max_findings:
        raise ValueError(
            f"prose finding count exceeds the {max_findings}-finding budget"
        )
    findings.append(finding)


def inspect_text(
    text: str,
    display_path: str,
    *,
    max_sentence_words: int = DEFAULT_SENTENCE_WORDS,
    max_findings: int = MAX_FINDINGS,
) -> list[Finding]:
    if max_findings < 0:
        raise ValueError("max_findings must not be negative")
    findings: list[Finding] = []
    for paragraph in _paragraphs(text):
        for sentence_start, sentence in _sentence_spans(paragraph.text):
            word_count = 0
            overflow_offset = 0
            for word in WORD_RE.finditer(sentence):
                word_count += 1
                if word_count == max_sentence_words + 1:
                    overflow_offset = word.start()
            if word_count > max_sentence_words:
                _retain_finding(
                    findings,
                    Finding(
                        path=display_path,
                        line=paragraph.source_line(
                            sentence_start + overflow_offset
                        ),
                        rule="long-sentence",
                        message=(
                            f"{word_count} words; consider splitting above "
                            f"{max_sentence_words}"
                        ),
                        excerpt=_excerpt(sentence),
                    ),
                    max_findings=max_findings,
                )
        for pattern, suggestion in DISCOURAGED:
            match = pattern.search(paragraph.text)
            if match:
                _retain_finding(
                    findings,
                    Finding(
                        path=display_path,
                        line=paragraph.source_line(match.start()),
                        rule="avoidable-phrase",
                        message=suggestion,
                        excerpt=_excerpt(paragraph.text),
                    ),
                    max_findings=max_findings,
                )
    return findings


def _discover_markdown_paths(
    root: Path,
    *,
    max_files: int,
    max_entries: int = MAX_DISCOVERY_ENTRIES,
    max_depth: int = MAX_DISCOVERY_DEPTH,
) -> list[Path]:
    if max_files < 0 or max_entries < 1 or max_depth < 0:
        raise ValueError(
            "the discovery file budget must be non-negative and the entry "
            "budget must be positive; the depth budget must be non-negative"
        )
    root = Path(os.path.abspath(root))
    matches: list[Path] = []
    entry_count = 0

    def walk(directory: Path, descriptor: int, depth: int) -> None:
        nonlocal entry_count
        if depth > max_depth:
            raise ValueError(
                "documentation discovery exceeds the "
                f"{max_depth}-directory depth budget"
            )
        before_directory = os.fstat(descriptor)
        child_directories: list[tuple[str, os.stat_result]] = []
        scan_target: int | Path = directory if os.name == "nt" else descriptor
        with os.scandir(scan_target) as iterator:
            for entry in iterator:
                entry_count += 1
                if entry_count > max_entries:
                    raise ValueError(
                        "documentation discovery exceeds the "
                        f"{max_entries}-entry budget"
                    )
                entry_stat = entry.stat(follow_symlinks=False)
                if _is_plain_directory(entry_stat):
                    child_directories.append((entry.name, entry_stat))
                    continue
                if not entry.name.endswith(".md"):
                    continue
                if len(matches) >= max_files:
                    raise ValueError(
                        "documentation discovery exceeds the "
                        f"{max_files}-file budget"
                    )
                matches.append(directory / entry.name)

        for child_name, expected_stat in sorted(child_directories):
            child = directory / child_name
            child_descriptor = _open_plain_directory_descriptor(
                child,
                expected_stat,
                parent_descriptor=descriptor,
                child_name=child_name,
            )
            try:
                walk(child, child_descriptor, depth + 1)
            finally:
                os.close(child_descriptor)

        if _file_snapshot(os.fstat(descriptor)) != _file_snapshot(
            before_directory
        ):
            raise ValueError(f"{directory}: changed during traversal")

    root_stat = root.lstat()
    root_descriptor = _open_plain_directory_descriptor(root, root_stat)
    try:
        walk(root, root_descriptor, 0)
    finally:
        os.close(root_descriptor)
    return sorted(matches)


def _default_paths() -> list[Path]:
    return [
        REPO_ROOT / "README.md",
        *_discover_markdown_paths(
            REPO_ROOT / "docs",
            max_files=MAX_FILES - 1,
        ),
    ]


def inspect_paths(
    paths: Iterable[Path],
    *,
    max_sentence_words: int = DEFAULT_SENTENCE_WORDS,
    allow_external_paths: bool = False,
) -> list[Finding]:
    materialized: list[Path] = []
    for path in paths:
        if len(materialized) >= MAX_FILES:
            raise ValueError(f"refusing to inspect more than {MAX_FILES} files")
        materialized.append(path)
    findings: list[Finding] = []
    for path in materialized:
        absolute = Path(os.path.abspath(path))
        supplied_stat = absolute.lstat()
        if not _is_plain_file(supplied_stat):
            raise ValueError(f"{path}: symlinks and non-regular files are not read")
        resolved = absolute.resolve(strict=True)
        inside_repository = resolved.is_relative_to(REPO_ROOT)
        if not inside_repository and not allow_external_paths:
            raise ValueError(
                f"{path}: resolves outside the repository; "
                "pass --allow-external-paths to inspect it explicitly"
            )
        resolved_stat = resolved.lstat()
        if (
            not _is_plain_file(resolved_stat)
            or _file_identity(supplied_stat) != _file_identity(resolved_stat)
        ):
            raise ValueError(f"{path}: changed or resolved through a symlink")
        display = (
            resolved.relative_to(REPO_ROOT).as_posix()
            if inside_repository
            else str(resolved)
        )
        if len(display.encode("utf-8")) > MAX_DISPLAY_PATH_BYTES:
            raise ValueError(
                f"display path exceeds the {MAX_DISPLAY_PATH_BYTES}-byte limit"
            )
        findings.extend(
            inspect_text(
                _read_bounded(resolved, display, resolved_stat),
                display,
                max_sentence_words=max_sentence_words,
                max_findings=MAX_FINDINGS - len(findings),
            )
        )
    return sorted(findings, key=lambda item: (item.path, item.line, item.rule))


def _terminal_safe(value: str) -> str:
    rendered: list[str] = []
    for character in value:
        if character.isprintable():
            rendered.append(character)
            continue
        codepoint = ord(character)
        escape = "u" if codepoint <= 0xFFFF else "U"
        width = 4 if escape == "u" else 8
        rendered.append(f"\\{escape}{codepoint:0{width}x}")
    return "".join(rendered)


def _text_report(findings: Sequence[Finding]) -> str:
    lines: list[str] = []
    for finding in findings:
        lines.extend(
            (
                (
                    f"{_terminal_safe(finding.path)}:{finding.line}: "
                    f"{_terminal_safe(finding.rule)}: "
                    f"{_terminal_safe(finding.message)}"
                ),
                f"  {_terminal_safe(finding.excerpt)}",
            )
        )
    lines.append(
        f"Advisory prose report: {len(findings)} finding(s). "
        "Review judgment is authoritative."
    )
    return "\n".join(lines)


def _bounded_terminal_error(value: str) -> str:
    safe = _terminal_safe(value)
    encoded = safe.encode("utf-8")
    if len(encoded) <= MAX_ERROR_BYTES:
        return safe
    suffix = b"..."
    prefix = encoded[: MAX_ERROR_BYTES - len(suffix)].decode(
        "utf-8", errors="ignore"
    )
    return prefix + suffix.decode("ascii")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    parser.add_argument(
        "--max-sentence-words",
        type=int,
        default=DEFAULT_SENTENCE_WORDS,
        help="Advisory sentence-length threshold (default: 35).",
    )
    parser.add_argument(
        "--fail-on-findings",
        action="store_true",
        help="Opt into a non-zero exit; this is deliberately not the default.",
    )
    parser.add_argument(
        "--allow-external-paths",
        action="store_true",
        help="Allow explicit regular-file inputs outside the repository.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.max_sentence_words < 10 or args.max_sentence_words > 200:
        print("--max-sentence-words must be between 10 and 200", file=sys.stderr)
        return 2
    try:
        findings = inspect_paths(
            args.paths or _default_paths(),
            max_sentence_words=args.max_sentence_words,
            allow_external_paths=args.allow_external_paths,
        )
    except (OSError, ValueError) as exc:
        print(
            _bounded_terminal_error(f"prose report failed: {exc}"),
            file=sys.stderr,
        )
        return 2

    if args.format == "json":
        output = json.dumps(
            {
                "schema": "boundver-prose-report/v1",
                "advisory": True,
                "finding_count": len(findings),
                "findings": [asdict(finding) for finding in findings],
            },
            indent=2,
            sort_keys=True,
        )
    else:
        output = _text_report(findings)
    if len(output.encode("utf-8")) > MAX_OUTPUT_BYTES:
        print("prose report exceeds its output byte budget", file=sys.stderr)
        return 2
    print(output)
    return 1 if findings and args.fail_on_findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
