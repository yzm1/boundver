"""Run one repository under several spellings and require a single answer.

About ninety obligations in the register compare boundver against boundver
under a different configuration: two source modes on a clean tree, a raw and a
canonical provider resolving the same declaration, the same config written as
JSON, YAML and TOML, a flag and its default, text output and JSON output. Each
names an axis that must not change the answer.

The awkward part of writing those by hand is not running the variants, it is
the failure message: with five spellings and two answers, a bare assertEqual
says only that two dicts differ. `assert_variants_agree` reports the partition
instead, so the output names which spellings agreed with which.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from tests._scenarios import SOURCE_MODES, Scenario

#: One spelling: a display name and a function that applies it to a scenario.
Variant = Tuple[str, Callable[[Scenario], None]]

_SRC = str(Path(__file__).resolve().parents[1] / "src")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def partition(results: Dict[str, Any]) -> List[List[str]]:
    """Group variant names by the answer they produced, largest group first."""
    groups: Dict[str, List[str]] = {}
    for name, value in results.items():
        groups.setdefault(_canonical(value), []).append(name)
    return sorted(groups.values(), key=lambda names: (-len(names), names))


def describe(results: Dict[str, Any]) -> str:
    """A failure message that names the split rather than dumping two blobs."""
    groups = partition(results)
    if len(groups) == 1:
        return "every spelling agreed"
    lines = [f"{len(groups)} distinct answers across {len(results)} spellings:"]
    for index, names in enumerate(groups, start=1):
        sample = _canonical(results[names[0]])
        if len(sample) > 400:
            sample = sample[:400] + "..."
        lines.append(f"  [{index}] {', '.join(names)}")
        lines.append(f"      {sample}")
    return "\n".join(lines)


def observe_variants(
    build: Callable[[], Scenario],
    variants: Sequence[Variant],
    observe: Callable[[Scenario], Any],
) -> Dict[str, Any]:
    """Build a fresh scenario per spelling and record what each produced.

    A fresh scenario per variant matters: several axes are spellings of the
    config file itself, and reusing one repository would leave the previous
    spelling committed in its history.
    """
    results: Dict[str, Any] = {}
    for name, apply in variants:
        with build() as scene:
            apply(scene)
            results[name] = observe(scene)
    return results


def assert_variants_agree(
    case,
    build: Callable[[], Scenario],
    variants: Sequence[Variant],
    observe: Callable[[Scenario], Any],
    subject: str,
) -> Dict[str, Any]:
    results = observe_variants(build, variants, observe)
    case.assertEqual(len(partition(results)), 1, f"{subject}: {describe(results)}")
    return results


def assert_source_modes_agree(case, scene: Scenario, subject: str = "source modes"):
    """On a clean tree every source mode must produce the identical lockfile.

    No field of the document records which mode produced it, so the comparison
    is the whole lockfile rather than a chosen projection.

    The clean tree is a precondition, and checking it is not ceremony. Setting
    an executable mode through the index under `core.filemode=true` leaves a
    tree that Git itself reports as modified on a filesystem with no execute
    bit, and the source modes then differ for a good reason. Without this
    check that case would read as a parity failure.
    """
    status = scene.git("status", "--porcelain")
    case.assertEqual(status, "", f"{subject}: tree is not clean:\n{status}")
    results = {mode: scene.generate(source=mode) for mode in SOURCE_MODES}
    case.assertEqual(len(partition(results)), 1, f"{subject}: {describe(results)}")
    return results


def run_cli(root: Path, *args: str) -> subprocess.CompletedProcess:
    """Invoke the installed CLI the way a user would."""
    env = os.environ.copy()
    env["PYTHONPATH"] = _SRC + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    return subprocess.run(
        [sys.executable, "-m", "boundver", *args],
        cwd=root,
        capture_output=True,
        text=True,
        env=env,
    )


def cli_json(root: Path, *args: str) -> Optional[dict]:
    """The JSON view a command produced, or None when it printed none."""
    result = run_cli(root, *args)
    if not result.stdout.strip():
        return None
    return json.loads(result.stdout)


def run_cli_in_process(root: Path, *args: str) -> subprocess.CompletedProcess:
    """Invoke the CLI inside this interpreter, so a patched limit applies.

    A subprocess is the better witness for anything a user can reach from a
    shell, and `run_cli` stays the default for that reason. But a ceiling
    expressed as a module constant cannot be lowered across a process
    boundary, and the ceilings worth testing are all far too high to reach
    with real inputs. This runner exists for those: it returns the same
    CompletedProcess shape so a test can be written either way.
    """
    import io
    from contextlib import redirect_stderr, redirect_stdout

    from boundver import core

    stdout, stderr = io.StringIO(), io.StringIO()
    previous_argv, previous_directory = sys.argv[:], Path.cwd()
    try:
        os.chdir(root)
        sys.argv = ["boundver", *args]
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                core.main()
            except SystemExit as exc:
                code = int(exc.code or 0)
            else:
                code = 0
    finally:
        sys.argv = previous_argv
        os.chdir(previous_directory)
    return subprocess.CompletedProcess(
        args=["boundver", *args],
        returncode=code,
        stdout=stdout.getvalue(),
        stderr=stderr.getvalue(),
    )
