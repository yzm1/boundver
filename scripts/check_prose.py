#!/usr/bin/env python3
"""Report high-confidence prose smells without turning style into a release gate."""

from __future__ import annotations

import argparse
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


def _file_identity(file_stat: os.stat_result) -> tuple[int, int]:
    return file_stat.st_dev, file_stat.st_ino


def _file_snapshot(
    file_stat: os.stat_result,
) -> tuple[int, int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def _is_plain_file(file_stat: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(file_stat, "st_file_attributes", 0)
    return stat.S_ISREG(file_stat.st_mode) and not (attributes & reparse_flag)


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

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            before_descriptor = os.fstat(stream.fileno())
            if not _is_plain_file(before_descriptor):
                raise ValueError(f"{display_path}: is not a regular file")
            if _file_identity(before_descriptor) != _file_identity(before_path):
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


def _paragraphs(text: str) -> Iterator[tuple[int, str]]:
    lines = text.splitlines()
    front_matter = bool(lines and lines[0].strip() == "---")
    in_fence = False
    fence_character = ""
    pending: list[str] = []
    start_line = 1

    def flush() -> tuple[int, str] | None:
        nonlocal pending
        if not pending:
            return None
        paragraph = " ".join(pending)
        pending = []
        return start_line, paragraph

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
        if not pending:
            start_line = line_number
        pending.append(cleaned)
        if starts_list_item:
            result = flush()
            if result:
                yield result

    result = flush()
    if result:
        yield result


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
    for line, paragraph in _paragraphs(text):
        for sentence in SENTENCE_END_RE.split(paragraph):
            word_count = len(WORD_RE.findall(sentence))
            if word_count > max_sentence_words:
                _retain_finding(
                    findings,
                    Finding(
                        path=display_path,
                        line=line,
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
            if pattern.search(paragraph):
                _retain_finding(
                    findings,
                    Finding(
                        path=display_path,
                        line=line,
                        rule="avoidable-phrase",
                        message=suggestion,
                        excerpt=_excerpt(paragraph),
                    ),
                    max_findings=max_findings,
                )
    return findings


def _discover_markdown_paths(
    root: Path,
    *,
    max_files: int,
    max_entries: int = MAX_DISCOVERY_ENTRIES,
) -> list[Path]:
    if max_files < 0 or max_entries < 1:
        raise ValueError(
            "the discovery file budget must be non-negative and the entry "
            "budget must be positive"
        )
    matches: list[Path] = []
    directories = [root]
    entry_count = 0
    while directories:
        directory = directories.pop()
        child_directories: list[Path] = []
        with os.scandir(directory) as iterator:
            for entry in iterator:
                entry_count += 1
                if entry_count > max_entries:
                    raise ValueError(
                        "documentation discovery exceeds the "
                        f"{max_entries}-entry budget"
                    )
                entry_stat = entry.stat(follow_symlinks=False)
                attributes = getattr(entry_stat, "st_file_attributes", 0)
                reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                is_reparse = bool(attributes & reparse_flag)
                if stat.S_ISDIR(entry_stat.st_mode) and not is_reparse:
                    child_directories.append(Path(entry.path))
                    continue
                if not entry.name.endswith(".md"):
                    continue
                if len(matches) >= max_files:
                    raise ValueError(
                        "documentation discovery exceeds the "
                        f"{max_files}-file budget"
                    )
                matches.append(Path(entry.path))
        directories.extend(reversed(sorted(child_directories)))
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
