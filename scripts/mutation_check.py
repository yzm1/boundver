#!/usr/bin/env python3
"""Run the curated release mutation catalog and report which faults survive.

Coverage answers whether a test executed a line. This answers the question the
register actually cares about: would the suite notice if the line were wrong.
An obligation marked asserted means a reader judged a test sufficient, and that
judgement is the register's most load-bearing claim. A mutant that survives
turns it into a measurement.

The public catalog is `spec/release-mutations.json`. It contains a bounded set
of high-risk faults selected from the private survey evidence. Each entry names
a fault, the tests that must kill it, and the obligations that rest on those
tests. The complete survey catalog remains private evidence because most of its
recipes became stale when the findings they exposed were remediated.

Exit codes follow the boundver convention: 0 when every mutant behaved as
declared, 1 when one did not, 2 when this script could not run the check.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "spec" / "release-mutations.json"
RECORD = "boundver-release-mutation-catalog/v1"
PRIVATE_RECORD = "boundver-mutation-catalog/v1"

MAX_CATALOG_BYTES = 256 * 1024
MAX_MUTANTS = 1_000
MAX_TEXT_CHARS = 64 * 1024
MAX_TESTS_PER_MUTANT = 32
MAX_OBLIGATIONS_PER_MUTANT = 64

OK = 0
FAILED = 1
USAGE = 2


def _read(path: Path) -> str:
    return io.open(path, encoding="utf-8", newline="").read()


def _write(path: Path, text: str) -> None:
    io.open(path, "w", encoding="utf-8", newline="").write(text)


def _unique_object(pairs: Sequence[Tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _safe_catalog_path(value: str, *, prefix: str, field: str) -> None:
    path = PurePosixPath(value)
    if (
        "\\" in value
        or path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.parts[0] != prefix
        or path.suffix != ".py"
    ):
        raise ValueError(
            f"{field} must be a relative {prefix}/*.py path without traversal"
        )


def _safe_test_selector(value: str, *, field: str) -> None:
    path, *nodes = value.split("::")
    _safe_catalog_path(path, prefix="tests", field=field)
    if any(
        not node or re.fullmatch(r"[A-Za-z0-9_.\[\]-]+", node) is None
        for node in nodes
    ):
        raise ValueError(f"{field} has an invalid pytest node selector")


def load_catalog(path: Path = CATALOG) -> dict:
    with path.open("rb") as handle:
        raw = handle.read(MAX_CATALOG_BYTES + 1)
    if len(raw) > MAX_CATALOG_BYTES:
        raise ValueError(
            f"{path} exceeds the {MAX_CATALOG_BYTES}-byte catalog limit"
        )
    try:
        document = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_object
        )
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path} is not UTF-8") from exc
    if not isinstance(document, dict) or document.get("record") not in {
        RECORD,
        PRIVATE_RECORD,
    }:
        raise ValueError(f"{path} is not a supported mutation catalog")
    mutants = document.get("mutants")
    if not isinstance(mutants, list) or not 1 <= len(mutants) <= MAX_MUTANTS:
        raise ValueError(
            f"{path} must contain between 1 and {MAX_MUTANTS} mutants"
        )
    identifiers = set()
    required_strings = ("id", "subsystem", "label", "file", "search", "replace")
    for index, mutant in enumerate(mutants):
        if not isinstance(mutant, dict):
            raise ValueError(f"mutant {index} must be an object")
        for field in required_strings:
            value = mutant.get(field)
            if not isinstance(value, str) or not value or len(value) > MAX_TEXT_CHARS:
                raise ValueError(f"mutant {index} has invalid {field}")
        identifier = mutant["id"]
        if identifier in identifiers:
            raise ValueError(f"duplicate mutant id {identifier!r}")
        identifiers.add(identifier)
        _safe_catalog_path(mutant["file"], prefix="src", field=f"{identifier}: file")
        if not mutant["file"].startswith("src/boundver/"):
            raise ValueError(f"{identifier}: file must be inside src/boundver")
        if mutant["search"] == mutant["replace"]:
            raise ValueError(f"{identifier}: search and replace must differ")
        expected = mutant.get("expected", "killed")
        if expected not in {"killed", "survives"}:
            raise ValueError(f"{identifier}: expected must be killed or survives")
        tests = mutant.get("tests")
        if (
            not isinstance(tests, list)
            or not 1 <= len(tests) <= MAX_TESTS_PER_MUTANT
            or any(not isinstance(item, str) or not item for item in tests)
        ):
            raise ValueError(f"{identifier}: tests must be a non-empty string array")
        for test in tests:
            _safe_test_selector(test, field=f"{identifier}: test")
        obligations = mutant.get("obligations")
        if (
            not isinstance(obligations, list)
            or not 1 <= len(obligations) <= MAX_OBLIGATIONS_PER_MUTANT
            or any(not isinstance(item, str) or not item for item in obligations)
            or len(set(obligations)) != len(obligations)
        ):
            raise ValueError(
                f"{identifier}: obligations must be a non-empty unique string array"
            )
    return document


def require_checkout_import() -> None:
    """Refuse to test mutations against a package imported from another tree."""
    probe = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            (
                "from pathlib import Path; import boundver; "
                "print(Path(boundver.__file__).resolve())"
            ),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    expected = (ROOT / "src" / "boundver" / "__init__.py").resolve()
    actual = probe.stdout.strip()
    if probe.returncode != 0 or not actual:
        raise ValueError("the selected Python cannot import boundver")
    if os.path.normcase(actual) != os.path.normcase(str(expected)):
        raise ValueError(
            "the selected Python imports boundver from a different checkout: "
            f"{actual} (expected {expected})"
        )


def working_tree_is_clean() -> bool:
    """Refuse to mutate a tree that holds work this script could lose."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and not result.stdout.strip()


def apply_mutant(mutant: dict) -> str:
    """Rewrite one file in place, returning the original text to restore."""
    target = ROOT / mutant["file"]
    original = _read(target)
    occurrences = original.count(mutant["search"])
    if occurrences != 1:
        raise ValueError(
            f"{mutant['id']}: anchor matches {occurrences} times in "
            f"{mutant['file']}; the catalog is stale"
        )
    _write(target, original.replace(mutant["search"], mutant["replace"]))
    return original


#: pytest's own exit codes. 1 means tests ran and some failed, which is the
#: only outcome that counts as a mutant being killed. 5 means nothing was
#: collected at all, which used to be scored as a kill because it is merely
#: "not zero" - a green run that proved nothing.
PYTEST_FAILED = 1
PYTEST_NO_TESTS = 5


def run_tests(paths: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", "-m", "pytest", "-q", "--no-header", *paths],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def require_tests_exist(mutant: dict) -> None:
    """Refuse a mutant whose named tests are not there to run.

    A catalog entry is a claim that these tests kill this fault. If the file
    has been renamed or deleted the claim is unverifiable, and running anyway
    produces a pass for the same reason an empty room is quiet.
    """
    absent = [
        name
        for name in mutant["tests"]
        if not (ROOT / name.split("::", 1)[0]).is_file()
    ]
    if absent:
        raise ValueError(
            f"{mutant['id']}: names {len(absent)} test path(s) that do not "
            f"exist ({', '.join(absent)}); the catalog is stale"
        )


def check(mutant: dict) -> Dict[str, object]:
    """Apply one mutant, run its tests, restore, and report what happened."""
    require_tests_exist(mutant)
    target = ROOT / mutant["file"]
    original = apply_mutant(mutant)
    try:
        result = run_tests(mutant["tests"])
    finally:
        _write(target, original)
    if result.returncode == PYTEST_NO_TESTS:
        raise ValueError(
            f"{mutant['id']}: its tests collected nothing, so the fault was "
            f"never put to them ({', '.join(mutant['tests'])}); the catalog is "
            "stale"
        )
    killed = result.returncode == PYTEST_FAILED
    expected_killed = mutant.get("expected", "killed") == "killed"
    summary = ""
    if result.stdout.strip():
        summary = result.stdout.strip().splitlines()[-1]
    return {
        "id": mutant["id"],
        "label": mutant["label"],
        "killed": killed,
        "as_declared": killed == expected_killed,
        "expected": "killed" if expected_killed else "survives",
        "summary": summary,
        "failing": [
            line.split("::")[-1]
            for line in result.stdout.splitlines()
            if line.startswith("FAILED")
        ],
    }


def select(catalog: dict, only: Optional[str], subsystem: Optional[str]) -> List[dict]:
    mutants = catalog["mutants"]
    if only:
        mutants = [m for m in mutants if m["id"] == only or only in m["label"]]
    if subsystem:
        mutants = [m for m in mutants if m.get("subsystem") == subsystem]
    return mutants


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--catalog",
        type=Path,
        default=CATALOG,
        help="mutation catalog to run (defaults to the curated release catalog)",
    )
    parser.add_argument("--list", action="store_true", help="print the catalog and stop")
    parser.add_argument("--only", help="run one mutant by id, or by a substring of its label")
    parser.add_argument("--subsystem", help="run every mutant for one subsystem")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="mutate even with uncommitted changes present",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        catalog = load_catalog(args.catalog)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return USAGE

    mutants = select(catalog, args.only, args.subsystem)
    if not mutants:
        print("ERROR: no mutant matched the selection", file=sys.stderr)
        return USAGE

    if args.list:
        for mutant in mutants:
            expected = mutant.get("expected", "killed")
            print(f"{mutant['id']}  [{expected}]  {mutant['label']}")
            print(f"    {mutant['file']} -> {', '.join(mutant['tests'])}")
        return OK

    try:
        require_checkout_import()
    except ValueError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return USAGE

    if not args.allow_dirty and not working_tree_is_clean():
        print(
            "ERROR: the working tree has uncommitted changes. This script edits "
            "source files in place and restores them, so a crash would lose "
            "them. Commit or stash first, or pass --allow-dirty.",
            file=sys.stderr,
        )
        return USAGE

    unexpected: List[Dict[str, object]] = []
    for mutant in mutants:
        print(f"running   {mutant['id']}  {mutant['label']}...", flush=True)
        try:
            outcome = check(mutant)
        except (OSError, ValueError) as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return USAGE
        verdict = "killed  " if outcome["killed"] else "survived"
        flag = "" if outcome["as_declared"] else "   NOT AS DECLARED"
        print(f"{verdict}  {outcome['id']}  {outcome['label']}{flag}")
        if outcome["summary"]:
            print(f"          {outcome['summary']}")
        for name in outcome["failing"][:3]:
            print(f"          {name}")
        if not outcome["as_declared"]:
            unexpected.append(outcome)
        sys.stdout.flush()

    print()
    killed = sum(1 for m in mutants if m.get("expected", "killed") == "killed")
    print(
        f"{len(mutants)} mutants: {killed} declared killable, "
        f"{len(mutants) - killed} declared equivalent, "
        f"{len(unexpected)} behaved otherwise"
    )
    if unexpected:
        for outcome in unexpected:
            expected = outcome["expected"]
            actual = "killed" if outcome["killed"] else "survived"
            print(f"  {outcome['id']}: declared {expected}, {actual}")
        return FAILED
    return OK


if __name__ == "__main__":
    sys.exit(main())
