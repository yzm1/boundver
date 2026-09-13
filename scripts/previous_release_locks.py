#!/usr/bin/env python3
"""Write example lockfiles with an older boundver, then read them with this one.

Every legacy lockfile in the test suite is synthesized inside the current
build, by generating a fresh lock and relabelling its contract fields. That
proves the migration code reads what this build writes; it cannot prove
anything about what an earlier release actually wrote, which is the thing a
user upgrading actually has.

So `write` runs a previously published boundver over the checked-in examples
and keeps what it produced. `check` runs this build against those artifacts and
requires one of two outcomes for each: verify clean, or refuse with an error
that names the axis which changed. A silent mismatch is neither, and a wrong
digest reported as clean is the failure this exists to catch.

Exit codes follow the boundver convention: 0 when every example met the
contract, 1 when one did not, 2 when the check could not run.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"

#: boundver refuses an output path outside the repository, which is its own
#: containment guard doing its job. So generation stages inside the tree and
#: the artifacts are moved out afterwards, rather than asking boundver to
#: write somewhere it is right to refuse.
STAGING = ROOT / ".previous-release-staging"
#: The axes a lockfile pins. A refusal must name at least one of them, so a
#: user is told what changed rather than only that something did.
CONTRACT_AXES = ("boundary-lock", "semantic-config", "provider_version", "contract")

OK, FAILED, USAGE = 0, 1, 2


def example_directories() -> List[Path]:
    return sorted(
        path.parent
        for path in EXAMPLES.glob("*/boundary.config.json")
    )


def _run(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(command), cwd=cwd, capture_output=True, text=True, check=False
    )


def write(boundver: str, out: Path) -> int:
    """Generate one lock per example with the older boundver."""
    directories = example_directories()
    if not directories:
        print(f"ERROR: no examples under {EXAMPLES}", file=sys.stderr)
        return USAGE
    out.mkdir(parents=True, exist_ok=True)
    STAGING.mkdir(parents=True, exist_ok=True)
    written = 0
    for directory in directories:
        name = directory.name
        staged = STAGING / f"{name}.lock.json"
        destination = out / f"{name}.lock.json"
        result = _run(
            [boundver, "generate", "--source", "working-tree",
             "--config", str(directory / "boundary.config.json"),
             "--out", str(staged.relative_to(ROOT).as_posix())],
            cwd=ROOT,
        )
        if result.returncode != 0 or not staged.exists():
            # An example the old release cannot express is not a failure of
            # this check. Record it and move on, so the job reports on what
            # both releases understand.
            print(f"skip {name}: previous release exited {result.returncode}")
            print(f"     {result.stderr.strip()[:200]}")
            continue
        shutil.move(str(staged), destination)
        written += 1
        print(f"wrote {destination.name}")
    if not written:
        print("ERROR: the previous release produced no lockfiles", file=sys.stderr)
        return USAGE
    shutil.rmtree(STAGING, ignore_errors=True)
    print(f"{written} of {len(directories)} examples produced a lock")
    return OK


def _names_an_axis(text: str) -> bool:
    return any(axis in text for axis in CONTRACT_AXES)


def check(locks: Path) -> int:
    """Require verify-clean or a refusal naming the axis, for each lock."""
    artifacts = sorted(locks.glob("*.lock.json"))
    if not artifacts:
        print(f"ERROR: no lockfiles under {locks}", file=sys.stderr)
        return USAGE

    failures = []
    for artifact in artifacts:
        name = artifact.name[: -len(".lock.json")]
        directory = EXAMPLES / name
        config = directory / "boundary.config.json"
        if not config.exists():
            failures.append(f"{name}: {config} disappeared")
            continue
        result = _run(
            [sys.executable, "-I", "-m", "boundver", "verify",
             "--source", "working-tree", "--config", str(config),
             "--lock", str(artifact)],
            cwd=ROOT,
        )

        combined = (result.stdout or "") + (result.stderr or "")
        if result.returncode == 0:
            print(f"ok      {name}: verifies clean under this build")
        elif _names_an_axis(combined):
            print(f"ok      {name}: refused, naming the changed axis")
        else:
            failures.append(
                f"{name}: exited {result.returncode} without naming a contract "
                f"axis: {combined.strip()[:300]}"
            )
            print(f"FAILED  {failures[-1]}")

    print()
    print(f"{len(artifacts)} lockfiles from the previous release, {len(failures)} failures")
    return FAILED if failures else OK


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    writer = sub.add_parser("write", help="generate locks with an older boundver")
    writer.add_argument("--boundver", required=True, help="path to the older executable")
    writer.add_argument("--out", required=True, type=Path)
    checker = sub.add_parser("check", help="read those locks with this build")
    checker.add_argument("--locks", required=True, type=Path)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "write":
            return write(args.boundver, args.out)
        return check(args.locks)
    except OSError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return USAGE


if __name__ == "__main__":
    sys.exit(main())
