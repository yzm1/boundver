#!/usr/bin/env python3
"""Fail closed unless every merge-critical CI dependency succeeded."""

from __future__ import annotations

import json
import os
import sys
from typing import Any


RESULTS_ENV = "BOUNDVER_REQUIRED_CI_NEEDS"
MAX_RESULTS_BYTES = 16 * 1024
REQUIRED_JOBS = (
    "test",
    "build",
    "action",
    "public-installations",
)


class RequiredCiGateError(ValueError):
    """The aggregate CI dependency result is absent or not all-successful."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise RequiredCiGateError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def validate_required_ci_results(raw: str) -> dict[str, str]:
    """Return normalized job results or raise on any non-success state."""
    if type(raw) is not str or not raw:
        raise RequiredCiGateError(f"{RESULTS_ENV} is missing or empty")
    try:
        encoded = raw.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RequiredCiGateError("CI dependency results are not valid UTF-8") from exc
    if len(encoded) > MAX_RESULTS_BYTES:
        raise RequiredCiGateError(
            f"CI dependency results exceed the {MAX_RESULTS_BYTES}-byte limit"
        )
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except RequiredCiGateError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise RequiredCiGateError("CI dependency results are not valid JSON") from exc
    if type(value) is not dict:
        raise RequiredCiGateError("CI dependency results must be a JSON object")

    expected = set(REQUIRED_JOBS)
    actual = set(value)
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing or unexpected:
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if unexpected:
            details.append("unexpected: " + ", ".join(unexpected))
        raise RequiredCiGateError(
            "CI dependency job set does not match the merge contract ("
            + "; ".join(details)
            + ")"
        )

    results: dict[str, str] = {}
    failed: list[str] = []
    for job in REQUIRED_JOBS:
        record = value[job]
        if type(record) is not dict or type(record.get("result")) is not str:
            raise RequiredCiGateError(
                f"CI dependency {job!r} has no string result"
            )
        result = record["result"]
        results[job] = result
        if result != "success":
            failed.append(f"{job}={result}")
    if failed:
        raise RequiredCiGateError(
            "merge-critical CI did not fully succeed: " + ", ".join(failed)
        )
    return results


def main() -> int:
    try:
        results = validate_required_ci_results(os.environ.get(RESULTS_ENV, ""))
    except RequiredCiGateError as exc:
        print(f"ERROR: required PR gate failed: {exc}", file=sys.stderr)
        return 1
    print(
        "Required PR gate passed: "
        + ", ".join(f"{job}={results[job]}" for job in REQUIRED_JOBS)
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
