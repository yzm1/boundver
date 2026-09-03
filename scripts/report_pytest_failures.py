#!/usr/bin/env python3
"""Render bounded, command-safe diagnostics from one pytest JUnit report."""

from __future__ import annotations

import argparse
import io
import os
import stat
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterator, Sequence


MAX_REPORT_BYTES = 16 * 1024 * 1024
MAX_TESTCASES = 100_000
MAX_XML_ELEMENTS = 300_000
MAX_XML_DEPTH = 128
MAX_XML_ATTRIBUTES_PER_ELEMENT = 64
MAX_FAILURES = 256
MAX_NAME_CHARS = 300
MAX_MESSAGE_CHARS = 300
MAX_BODY_CHARS = 500
READ_CHUNK_BYTES = 64 * 1024


class ReportError(ValueError):
    """The JUnit report cannot be read or rendered safely."""


def _is_windows_reparse_point(identity: os.stat_result) -> bool:
    attributes = getattr(identity, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _changed(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        or before.st_mode != after.st_mode
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    )


def _read_report(path: Path) -> str:
    try:
        initial = path.lstat()
    except OSError as error:
        raise ReportError(f"cannot inspect JUnit report: {error}") from error
    if not stat.S_ISREG(initial.st_mode) or _is_windows_reparse_point(initial):
        raise ReportError("JUnit report must be a regular non-reparse file")
    if initial.st_size > MAX_REPORT_BYTES:
        raise ReportError(
            f"JUnit report exceeds the {MAX_REPORT_BYTES}-byte limit"
        )

    content = bytearray()
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or _changed(initial, opened):
                raise ReportError("JUnit report changed while being opened")
            while True:
                remaining = MAX_REPORT_BYTES - len(content)
                chunk = stream.read(min(READ_CHUNK_BYTES, remaining + 1))
                if not chunk:
                    break
                if len(chunk) > remaining:
                    raise ReportError(
                        f"JUnit report exceeds the {MAX_REPORT_BYTES}-byte limit"
                    )
                content.extend(chunk)
            finished = os.fstat(stream.fileno())
        current = path.lstat()
    except ReportError:
        raise
    except OSError as error:
        raise ReportError(f"cannot read JUnit report: {error}") from error
    if (
        not stat.S_ISREG(finished.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or _is_windows_reparse_point(current)
        or _changed(opened, finished)
        or _changed(finished, current)
    ):
        raise ReportError("JUnit report changed while being read")
    try:
        return bytes(content).decode("utf-8")
    except UnicodeDecodeError as error:
        raise ReportError("JUnit report is not valid UTF-8") from error


def _safe_text(value: object, max_chars: int) -> str:
    text = str(value)
    truncated = len(text) > max_chars
    text = text[:max_chars]
    rendered: list[str] = []
    for character in text:
        codepoint = ord(character)
        if character.isprintable() and character not in "\r\n":
            rendered.append(character)
        elif codepoint <= 0xFF:
            rendered.append(f"\\x{codepoint:02x}")
        elif codepoint <= 0xFFFF:
            rendered.append(f"\\u{codepoint:04x}")
        else:
            rendered.append(f"\\U{codepoint:08x}")
    if truncated:
        rendered.append("...")
    return "".join(rendered)


def _workflow_data(value: object, max_chars: int) -> str:
    return _safe_text(value, max_chars).replace("%", "%25")


def _workflow_property(value: object, max_chars: int) -> str:
    return (
        _workflow_data(value, max_chars)
        .replace(":", "%3A")
        .replace(",", "%2C")
    )


def _iter_testcases(text: str) -> Iterator[ET.Element]:
    """Yield testcases without retaining one attacker-sized XML tree."""
    depth = 0
    elements = 0
    open_testcases = 0
    try:
        # DTD/entity declarations are rejected before this call, input bytes
        # are bounded, and start events enforce element/depth/attribute work
        # budgets before any report data is rendered.
        events = ET.iterparse(  # nosec B314
            io.StringIO(text),
            events=("start", "end"),
        )
        for event, element in events:
            is_testcase = element.tag == "testcase"
            if event == "start":
                depth += 1
                elements += 1
                if depth > MAX_XML_DEPTH:
                    raise ReportError(
                        f"JUnit XML exceeds the {MAX_XML_DEPTH}-level depth limit"
                    )
                if elements > MAX_XML_ELEMENTS:
                    raise ReportError(
                        "JUnit XML exceeds the "
                        f"{MAX_XML_ELEMENTS}-element limit"
                    )
                if len(element.attrib) > MAX_XML_ATTRIBUTES_PER_ELEMENT:
                    raise ReportError(
                        "JUnit XML element exceeds the "
                        f"{MAX_XML_ATTRIBUTES_PER_ELEMENT}-attribute limit"
                    )
                if is_testcase:
                    open_testcases += 1
                continue

            if is_testcase:
                yield element
                open_testcases -= 1
            if open_testcases == 0:
                element.clear()
            depth -= 1
    except ET.ParseError as error:
        raise ReportError(f"cannot parse JUnit XML: {error}") from error


def report(path: Path) -> int:
    text = _read_report(path)
    folded = text.casefold()
    if "<!doctype" in folded or "<!entity" in folded:
        raise ReportError("JUnit report must not contain DTD or entity declarations")
    testcases = 0
    failures = 0
    omitted = 0
    for testcase in _iter_testcases(text):
        testcases += 1
        if testcases > MAX_TESTCASES:
            raise ReportError(
                f"JUnit report exceeds the {MAX_TESTCASES}-testcase limit"
            )
        for child in testcase:
            if child.tag not in {"failure", "error"}:
                continue
            if failures >= MAX_FAILURES:
                omitted += 1
                continue
            name = testcase.attrib.get("name", "?")
            message = child.attrib.get("message", "")
            body = child.text or ""
            print(
                f"::error title={_workflow_property(name, MAX_NAME_CHARS)}::"
                f"{_workflow_data(message, MAX_MESSAGE_CHARS)}"
            )
            print(f"FAIL: {_safe_text(name, MAX_NAME_CHARS)}", flush=True)
            print(
                f"MESSAGE: {_safe_text(message, MAX_MESSAGE_CHARS)}",
                flush=True,
            )
            print(f"DETAIL: {_safe_text(body, MAX_BODY_CHARS)}", flush=True)
            print("---")
            failures += 1
    if omitted:
        print(f"Additional failures omitted: {omitted}", flush=True)
    print(f"Total failures: {failures + omitted}", flush=True)
    return failures + omitted


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report(args.report)
    except (OSError, ReportError, ValueError) as error:
        detail = _safe_text(error, MAX_MESSAGE_CHARS)
        print(
            f"::error title=Pytest%20report::"
            f"{_workflow_data(detail, MAX_MESSAGE_CHARS)}"
        )
        print(f"Could not report pytest failures: {detail}", flush=True)
    # This reporter runs only after an earlier CI failure. Its diagnostics must
    # never replace that authoritative step result with a parser-specific one.
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
