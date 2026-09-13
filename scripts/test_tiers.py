#!/usr/bin/env python3
"""Select and run the bounded PR, exhaustive, or complete test tier.

The committed catalog names deliberately expensive regression files. Everything
else, including every newly added test file, belongs to the required core tier
by default. That fail-safe default prevents a new regression test from silently
escaping pull-request CI.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "spec" / "test-tiers.json"
ASSURANCE_SUMMARY = ROOT / "spec" / "testing-obligations-summary.json"
RELEASE_MUTATIONS = ROOT / "spec" / "release-mutations.json"
RECORD = "boundver-test-tiers/v1"
ASSURANCE_RECORD = "boundver-testing-obligations-summary/v1"
MUTATION_RECORD = "boundver-release-mutation-catalog/v1"
MAX_CATALOG_BYTES = 128 * 1024
MAX_TEST_FILES = 1_000
MAX_PATH_BYTES = 4_096
TIERS = ("core", "exhaustive", "all")

OK = 0
FAILED = 1
USAGE = 2


class TestTierError(ValueError):
    """The test-tier catalog or requested selection is invalid."""

    __test__ = False


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise TestTierError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _canonical_test_path(value: object) -> str:
    if type(value) is not str:
        raise TestTierError("test-tier paths must be strings")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise TestTierError("test-tier path is not valid UTF-8") from exc
    parts = value.split("/")
    if (
        not encoded
        or len(encoded) > MAX_PATH_BYTES
        or "\\" in value
        or value.startswith("/")
        or any(part in ("", ".", "..") for part in parts)
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
        or len(parts) != 2
        or parts[0] != "tests"
        or not parts[1].startswith("test_")
        or not parts[1].endswith(".py")
    ):
        raise TestTierError(f"invalid test-tier path {value!r}")
    return value


def _read_catalog(path: Path, *, label: str = "test-tier catalog") -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_CATALOG_BYTES + 1)
    except OSError as exc:
        raise TestTierError(f"cannot read {label} {path}") from exc
    if len(raw) > MAX_CATALOG_BYTES:
        raise TestTierError(
            f"{label} exceeds the {MAX_CATALOG_BYTES}-byte limit"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TestTierError(f"{label} is not UTF-8") from exc
    try:
        value = json.loads(text, object_pairs_hook=_unique_object)
    except TestTierError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise TestTierError(f"{label} is not valid JSON") from exc
    if type(value) is not dict:
        raise TestTierError(f"{label} must be an object")
    return value


def load_assurance_counts(
    summary_path: Path = ASSURANCE_SUMMARY,
    mutation_path: Path = RELEASE_MUTATIONS,
) -> dict[str, int]:
    summary = _read_catalog(summary_path, label="assurance summary")
    if summary.get("record") != ASSURANCE_RECORD:
        raise TestTierError(
            f"assurance summary is not a {ASSURANCE_RECORD} document"
        )
    source = summary.get("source")
    dispositions = summary.get("dispositions")
    if type(source) is not dict or type(dispositions) is not dict:
        raise TestTierError("assurance summary is missing source or dispositions")

    fields = {
        "total": source.get("survey_obligations"),
        "asserted": source.get("survey_asserted"),
        "examples_only": source.get("survey_examples_only"),
        "uncovered": source.get("survey_uncovered"),
        "unassessed": source.get("survey_unassessed"),
        "survey_expected_failures": source.get("survey_expected_failures"),
        "remaining_expected_failures": dispositions.get(
            "remaining_expected_failures"
        ),
        "release_mutants": source.get("release_catalog_mutants"),
    }
    if any(type(value) is not int or value < 0 for value in fields.values()):
        raise TestTierError("assurance counts must be non-negative integers")
    if fields["total"] != sum(
        fields[name]
        for name in ("asserted", "examples_only", "uncovered", "unassessed")
    ):
        raise TestTierError("survey coverage counts do not equal the obligation total")

    mutations = _read_catalog(mutation_path, label="release mutation catalog")
    if mutations.get("record") != MUTATION_RECORD:
        raise TestTierError(
            f"release mutation catalog is not a {MUTATION_RECORD} document"
        )
    entries = mutations.get("mutants")
    if type(entries) is not list or len(entries) != fields["release_mutants"]:
        raise TestTierError(
            "release mutation catalog count disagrees with the assurance summary"
        )
    return fields


def repository_test_files(root: Path = ROOT) -> tuple[str, ...]:
    tests = root / "tests"
    if not tests.is_dir():
        raise TestTierError(f"test directory does not exist: {tests}")
    paths = tuple(
        sorted(
            candidate.relative_to(root).as_posix()
            for candidate in tests.glob("test_*.py")
            if candidate.is_file()
        )
    )
    if not paths or len(paths) > MAX_TEST_FILES:
        raise TestTierError(
            f"repository test-file count must be between 1 and {MAX_TEST_FILES}"
        )
    return paths


def load_tiers(
    path: Path = CATALOG,
    *,
    root: Path = ROOT,
) -> dict[str, tuple[str, ...]]:
    document = _read_catalog(path)
    if set(document) != {"record", "exhaustive_files"}:
        raise TestTierError("test-tier catalog has unexpected fields")
    if document.get("record") != RECORD:
        raise TestTierError(f"test-tier catalog is not a {RECORD} document")
    raw_exhaustive = document.get("exhaustive_files")
    if type(raw_exhaustive) is not list:
        raise TestTierError("exhaustive_files must be an array")
    if len(raw_exhaustive) > MAX_TEST_FILES:
        raise TestTierError(
            f"exhaustive_files exceeds the {MAX_TEST_FILES}-entry limit"
        )
    exhaustive = tuple(_canonical_test_path(item) for item in raw_exhaustive)
    if exhaustive != tuple(sorted(set(exhaustive))):
        raise TestTierError("exhaustive_files must be sorted and unique")

    all_files = repository_test_files(root)
    all_set = set(all_files)
    missing = tuple(path for path in exhaustive if path not in all_set)
    if missing:
        preview = ", ".join(missing[:3])
        raise TestTierError(f"exhaustive test files do not exist: {preview}")

    exhaustive_set = set(exhaustive)
    core = tuple(path for path in all_files if path not in exhaustive_set)
    if not core or not exhaustive:
        raise TestTierError("core and exhaustive tiers must both be non-empty")
    return {"core": core, "exhaustive": exhaustive, "all": all_files}


def pytest_command(
    tier: str,
    pytest_args: Sequence[str],
    *,
    path: Path = CATALOG,
    root: Path = ROOT,
) -> tuple[str, ...]:
    if tier not in TIERS:
        raise TestTierError(f"unknown test tier {tier!r}")
    files = load_tiers(path, root=root)[tier]
    arguments = list(pytest_args)
    if arguments[:1] == ["--"]:
        arguments.pop(0)
    return (
        sys.executable,
        "-I",
        "-m",
        "pytest",
        *arguments,
        "--",
        *files,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("check", help="validate and summarize the tier catalog")

    listing = subparsers.add_parser("list", help="print files in one tier")
    listing.add_argument("tier", choices=TIERS)

    run = subparsers.add_parser("run", help="run pytest for one tier")
    run.add_argument("tier", choices=TIERS)
    run.add_argument("pytest_args", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        tiers = load_tiers()
        if args.command == "check":
            assurance = load_assurance_counts()
            print(
                f"{len(tiers['all'])} test files: "
                f"{len(tiers['core'])} core, "
                f"{len(tiers['exhaustive'])} exhaustive"
            )
            print(
                f"{assurance['total']} surveyed obligations: "
                f"{assurance['asserted']} asserted, "
                f"{assurance['examples_only']} example-only, "
                f"{assurance['uncovered']} uncovered, "
                f"{assurance['unassessed']} unassessed; "
                f"expected failures {assurance['remaining_expected_failures']} current "
                f"of {assurance['survey_expected_failures']} retired; "
                f"{assurance['release_mutants']} release mutants require kills"
            )
            return OK
        if args.command == "list":
            print("\n".join(tiers[args.tier]))
            return OK
        command = pytest_command(args.tier, args.pytest_args)
    except TestTierError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return USAGE

    result = subprocess.run(command, cwd=ROOT, check=False)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
