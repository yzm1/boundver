#!/usr/bin/env python3
"""Report high-confidence prose smells without turning style into a release gate."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
MAX_FILES = 512
MAX_FILE_BYTES = 2 * 1024 * 1024
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


def _read_bounded(path: Path) -> str:
    with path.open("rb") as stream:
        payload = stream.read(MAX_FILE_BYTES + 1)
    if len(payload) > MAX_FILE_BYTES:
        raise ValueError(f"{path}: exceeds {MAX_FILE_BYTES} bytes")
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path}: is not valid UTF-8") from exc


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


def inspect_text(
    text: str,
    display_path: str,
    *,
    max_sentence_words: int = DEFAULT_SENTENCE_WORDS,
) -> list[Finding]:
    findings: list[Finding] = []
    for line, paragraph in _paragraphs(text):
        for sentence in SENTENCE_END_RE.split(paragraph):
            word_count = len(WORD_RE.findall(sentence))
            if word_count > max_sentence_words:
                findings.append(
                    Finding(
                        path=display_path,
                        line=line,
                        rule="long-sentence",
                        message=(
                            f"{word_count} words; consider splitting above "
                            f"{max_sentence_words}"
                        ),
                        excerpt=_excerpt(sentence),
                    )
                )
        for pattern, suggestion in DISCOURAGED:
            if pattern.search(paragraph):
                findings.append(
                    Finding(
                        path=display_path,
                        line=line,
                        rule="avoidable-phrase",
                        message=suggestion,
                        excerpt=_excerpt(paragraph),
                    )
                )
    return findings


def _default_paths() -> list[Path]:
    return [REPO_ROOT / "README.md", *sorted((REPO_ROOT / "docs").rglob("*.md"))]


def inspect_paths(
    paths: Iterable[Path],
    *,
    max_sentence_words: int = DEFAULT_SENTENCE_WORDS,
) -> list[Finding]:
    materialized = list(paths)
    if len(materialized) > MAX_FILES:
        raise ValueError(f"refusing to inspect more than {MAX_FILES} files")
    findings: list[Finding] = []
    for path in materialized:
        resolved = path.resolve(strict=True)
        display = (
            resolved.relative_to(REPO_ROOT).as_posix()
            if resolved.is_relative_to(REPO_ROOT)
            else str(resolved)
        )
        findings.extend(
            inspect_text(
                _read_bounded(resolved),
                display,
                max_sentence_words=max_sentence_words,
            )
        )
    return sorted(findings, key=lambda item: (item.path, item.line, item.rule))


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
        )
    except (OSError, ValueError) as exc:
        print(f"prose report failed: {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        print(
            json.dumps(
                {
                    "schema": "boundver-prose-report/v1",
                    "advisory": True,
                    "finding_count": len(findings),
                    "findings": [asdict(finding) for finding in findings],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        for finding in findings:
            print(
                f"{finding.path}:{finding.line}: {finding.rule}: "
                f"{finding.message}\n  {finding.excerpt}"
            )
        print(
            f"Advisory prose report: {len(findings)} finding(s). "
            "Review judgment is authoritative."
        )
    return 1 if findings and args.fail_on_findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
