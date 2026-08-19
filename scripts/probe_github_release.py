#!/usr/bin/env python3
"""Classify the public GitHub Release state for a release preflight.

This sidecar intentionally uses only the Python standard library.  The release
workflow executes it from the reviewed control checkout, not from the candidate
checkout, and gives it only the response captured by a preceding read-only API
request.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import stat
import sys
from pathlib import Path
from typing import Optional, Sequence


HTTP_STATUS_RE = re.compile(r"(?m)^HTTP/\S+\s+(\d{3})\b")
API_EXIT_RE = re.compile(r"[0-9]+")
RESPONSE_PREFIX = "github-release-response."
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_NAME_BYTES = 255
MAX_JSON_INTEGER_DIGITS = 4_300
MAX_JSON_NUMBER_CHARS = MAX_JSON_INTEGER_DIGITS + 32
_READ_CHUNK_BYTES = 64 * 1024


class ReleaseProbeError(ValueError):
    """The captured response or its invocation metadata is unsafe or malformed."""


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


def _response_path(response: Path, runner_temp: Path) -> Path:
    try:
        runner_identity = runner_temp.lstat()
        parent_identity = response.parent.lstat()
        runner_root = runner_temp.resolve(strict=True)
        response_parent = response.parent.resolve(strict=True)
    except OSError as exc:
        raise ReleaseProbeError("GitHub Release probe response path is unsafe") from exc
    if (
        not stat.S_ISDIR(runner_identity.st_mode)
        or _is_windows_reparse_point(runner_identity)
        or not stat.S_ISDIR(parent_identity.st_mode)
        or _is_windows_reparse_point(parent_identity)
        or (runner_identity.st_dev, runner_identity.st_ino)
        != (parent_identity.st_dev, parent_identity.st_ino)
        or response_parent != runner_root
        or not response.name.startswith(RESPONSE_PREFIX)
        or len(response.name.encode("utf-8", "surrogatepass"))
        > MAX_RESPONSE_NAME_BYTES
    ):
        raise ReleaseProbeError("GitHub Release probe response path is unsafe")
    candidate = runner_root / response.name
    try:
        identity = candidate.lstat()
    except OSError as exc:
        raise ReleaseProbeError("GitHub Release probe response path is unsafe") from exc
    if not stat.S_ISREG(identity.st_mode) or _is_windows_reparse_point(identity):
        raise ReleaseProbeError("GitHub Release probe response path is unsafe")
    return candidate


def _read_response(path: Path) -> str:
    """Read one stable response with a hard ceiling and sentinel byte."""
    try:
        initial = path.lstat()
        if not stat.S_ISREG(initial.st_mode) or _is_windows_reparse_point(initial):
            raise ReleaseProbeError("GitHub Release probe response path is unsafe")
        if initial.st_size > MAX_RESPONSE_BYTES:
            raise ReleaseProbeError(
                "GitHub Release probe response exceeds the "
                f"{MAX_RESPONSE_BYTES}-byte limit"
            )
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or _changed(initial, opened):
                raise ReleaseProbeError(
                    "GitHub Release probe response changed while opening"
                )
            content = bytearray()
            while True:
                remaining = MAX_RESPONSE_BYTES - len(content)
                chunk = stream.read(min(_READ_CHUNK_BYTES, remaining + 1))
                if not chunk:
                    break
                if len(chunk) > remaining:
                    raise ReleaseProbeError(
                        "GitHub Release probe response exceeds the "
                        f"{MAX_RESPONSE_BYTES}-byte limit"
                    )
                content.extend(chunk)
            finished = os.fstat(stream.fileno())
        current = path.lstat()
    except FileNotFoundError as exc:
        raise ReleaseProbeError(
            "GitHub Release probe response disappeared while reading"
        ) from exc
    if (
        not stat.S_ISREG(current.st_mode)
        or _is_windows_reparse_point(current)
        or _changed(opened, finished)
        or _changed(finished, current)
        or finished.st_size != len(content)
    ):
        raise ReleaseProbeError(
            "GitHub Release probe response changed while reading"
        )
    return bytes(content).decode("utf-8")


def parse_response(response: str) -> tuple[int, str]:
    """Return the final HTTP status and absent/draft/public/error state."""

    normalized = response.replace("\r\n", "\n").replace("\r", "\n")
    status_matches = list(HTTP_STATUS_RE.finditer(normalized))
    if not status_matches:
        raise ReleaseProbeError("GitHub Release probe returned no HTTP status")

    status = int(status_matches[-1].group(1))
    if status == 404:
        return status, "absent"
    if status != 200:
        return status, "error"

    body_start = normalized.find("\n\n", status_matches[-1].end())
    if body_start < 0:
        raise ReleaseProbeError("GitHub Release probe returned no response body")
    try:
        payload = _load_json(normalized[body_start + 2 :])
    except (TypeError, ValueError, RecursionError, OverflowError) as error:
        raise ReleaseProbeError(
            f"GitHub Release probe returned invalid JSON: {error}"
        ) from error
    if not isinstance(payload, dict) or type(payload.get("draft")) is not bool:
        raise ReleaseProbeError(
            "GitHub Release probe returned no boolean draft state"
        )
    return status, "draft" if payload["draft"] else "public"


def release_state(api_exit_text: str, response: str) -> tuple[str, str]:
    """Validate the command result and return its workflow state and message."""

    if len(api_exit_text) > 3 or API_EXIT_RE.fullmatch(api_exit_text) is None:
        raise ReleaseProbeError("GitHub Release probe exit status is malformed")
    api_exit = int(api_exit_text)
    if api_exit > 255:
        raise ReleaseProbeError("GitHub Release probe exit status is malformed")
    status, state = parse_response(response)

    if status == 404:
        return "absent", (
            "No GitHub Release exists yet; draft reconciliation remains downstream."
        )
    if api_exit != 0 or status != 200:
        raise ReleaseProbeError(
            f"GitHub Release probe failed with HTTP {status}"
        )
    if state == "draft":
        return "draft", (
            "A draft GitHub Release exists; exact draft reconciliation remains "
            "downstream."
        )
    if state != "public":  # pragma: no cover - parse_response constrains the state
        raise ReleaseProbeError(
            f"GitHub Release probe returned unexpected state: {state}"
        )
    return "public", "A public GitHub Release exists; verifying its exact bytes."


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-exit", required=True)
    parser.add_argument("--response", required=True, type=Path)
    parser.add_argument("--runner-temp", required=True, type=Path)
    parser.add_argument("--github-output", required=True, type=Path)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        response_path = _response_path(args.response, args.runner_temp)
        response = _read_response(response_path)
        state, message = release_state(args.api_exit, response)
        with args.github_output.open("a", encoding="utf-8", newline="\n") as output:
            output.write(f"release-state={state}\n")
    except (OSError, UnicodeError, ReleaseProbeError) as error:
        print(str(error), file=sys.stderr)
        return 1

    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
