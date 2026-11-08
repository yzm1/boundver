#!/usr/bin/env python3
"""Profile actual HEAD generation, including its optional working-tree warning.

Synthetic, local and offline. In-process CLI measurement excludes interpreter
startup, OS cache flushing and final publication (--dry-run). The instrumented
counterfactual suppresses ONLY the advisory helper, not any integrity check.
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import statistics
import sys
import tempfile
from unittest.mock import patch
import argparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import benchmark_adoption as adoption  # noqa: E402
from boundver import core, _hashing  # noqa: E402

runtime = adoption.runtime


def add_unowned_files(root: Path, count: int, payload_bytes: int = 4096) -> None:
    if not 0 <= count <= 8000 or not 0 <= payload_bytes <= 65536 or count * (payload_bytes + 32) > 256 * 1024 * 1024:
        raise ValueError("Outside-file fixture exceeds its count, file-size or 256 MiB budget")
    if count == 0:
        return
    directory = root / "unowned-fixture"
    directory.mkdir()
    for index in range(count):
        (directory / f"file-{index:04d}.dat").write_bytes(
            f"file {index}\n".encode("ascii") + b"x" * payload_bytes,
        )
    runtime._run(root, "add", "--all")
    runtime._run(root, "commit", "-m", "tracked files outside declared components")


def measure(root: Path, mode: str) -> dict:
    if mode not in {"default", "quiet", "counterfactual-no-advisory", "inspect"}:
        raise ValueError("Unknown CLI profile mode")
    metrics = {"advisory_calls": 0, "advisory_completed": False, "advisory_failure_type": None,
               "working_tree_reads": 0,
               "working_tree_bytes": 0, "unowned_working_tree_reads": 0,
               "unowned_working_tree_bytes": 0, "phases": []}
    original_advisory = core.dirty_component_paths
    original_generate = core.generate_lockfile
    original_read = _hashing._read_path_content

    def advisory(*args, **kwargs):
        metrics["advisory_calls"] += 1
        if mode == "counterfactual-no-advisory":
            return []
        try:
            value, phase = runtime._measure("working_tree_advisory", lambda: original_advisory(*args, **kwargs))
        except BaseException as exc:
            metrics["advisory_failure_type"] = type(exc).__name__
            raise
        metrics["advisory_completed"] = True
        metrics["phases"].append(phase)
        return value

    def generate(*args, **kwargs):
        value, phase = runtime._measure("generate_fingerprints", lambda: original_generate(*args, **kwargs))
        metrics["phases"].append(phase)
        metrics["lock_sha256"] = hashlib.sha256(runtime.dump_lockfile(value).encode("utf-8")).hexdigest()
        return value

    def read(repo_root, path, source, *args, **kwargs):
        value = original_read(repo_root, path, source, *args, **kwargs)
        if source == "working-tree":
            metrics["working_tree_reads"] += 1
            metrics["working_tree_bytes"] += len(value)
            if not path.relative_to(repo_root).as_posix().startswith("components/"):
                metrics["unowned_working_tree_reads"] += 1
                metrics["unowned_working_tree_bytes"] += len(value)
        return value

    stdout, stderr = io.StringIO(), io.StringIO()
    previous_directory, previous_argv = Path.cwd(), sys.argv[:]
    command = ["boundver", "generate", "--source", "head", "--dry-run"]
    if mode == "quiet":
        command.append("--quiet")
    if mode == "inspect":
        command.append("--inspect-working-tree")
    try:
        os.chdir(root)
        sys.argv = command
        with (redirect_stdout(stdout), redirect_stderr(stderr),
              patch.object(core, "dirty_component_paths", advisory),
              patch.object(core, "generate_lockfile", generate),
              patch.object(_hashing, "_read_path_content", read)):
            def invoke():
                try:
                    core.main()
                except SystemExit as exc:
                    return int(exc.code or 0)
                return 0
            status, total = runtime._measure("complete_cli", invoke)
    finally:
        os.chdir(previous_directory)
        sys.argv = previous_argv
    return {**metrics, "mode": mode, "exit_code": status, "total": total,
            "stdout_bytes": len(stdout.getvalue().encode("utf-8")),
            "stderr_bytes": len(stderr.getvalue().encode("utf-8"))}


def profile(root: Path, count: int, outside: int, runs: int, inspect: bool,
            outside_payload_bytes: int = 4096, storage_label: str = "unspecified") -> dict:
    adoption.create_fixture(root, count)
    add_unowned_files(root, outside, outside_payload_bytes)
    modes = ["default", "quiet", "counterfactual-no-advisory"] + (["inspect"] if inspect else [])
    results = {}
    for mode in modes:
        print(f"Profiling {mode}: first invocation", file=sys.stderr, flush=True)
        first = measure(root, mode)
        repeated = []
        for index in range(runs):
            print(f"Profiling {mode}: repeat {index + 1}/{runs}", file=sys.stderr, flush=True)
            repeated.append(measure(root, mode))
        if any(row["exit_code"] for row in [first, *repeated]):
            raise RuntimeError(f"CLI profile operation failed: {mode}")
        wall = [row["total"]["wall_seconds"] for row in repeated]
        results[mode] = {"first": first, "repeated": repeated,
                         "median_wall_seconds": statistics.median(wall),
                         "min_wall_seconds": min(wall), "max_wall_seconds": max(wall),
                         "population_stdev_wall_seconds": statistics.pstdev(wall)}
    return {"schema_version": 1, "components": count, "outside_files": outside,
            "outside_payload_bytes_per_file": outside_payload_bytes,
            "storage_label": storage_label,
            "host": {"platform": platform.platform(), "python": platform.python_version(),
                     "git": runtime._run(root, "--version")},
            "synthetic": True, "counterfactual_disables_only_advisory": True,
            "measurement": __doc__, "results": results}


def contract_violations(result: dict) -> list[str]:
    """Measured fixture contract, not a latency promise for arbitrary repos."""
    violations = []
    results = result["results"]
    default, quiet = results["default"], results["quiet"]
    process_limit = 20 + 2 * (result["components"] // 4)
    for row in [default["first"], *default["repeated"]]:
        if row["advisory_calls"] or row["working_tree_reads"]:
            violations.append("Default HEAD generation inspected the working tree")
        if row["total"]["git_commands"].get("write-tree", 0):
            violations.append("Default HEAD generation captured the shared index")
        if row["total"]["git_processes"] > process_limit:
            violations.append(f"Default HEAD generation exceeded {process_limit} Git processes")
    time_limit = 1.5 * quiet["median_wall_seconds"] + 0.25
    if default["median_wall_seconds"] > time_limit:
        violations.append("Default HEAD repeated median exceeded 1.5 * quiet median + 0.25 seconds")
    digests = {row.get("lock_sha256") for case in results.values()
               for row in [case["first"], *case["repeated"]]}
    if any(row["exit_code"] for case in results.values()
           for row in [case["first"], *case["repeated"]]):
        violations.append("A generation mode failed")
    if len(digests) != 1 or None in digests:
        violations.append("Generation modes did not compute identical lock bytes")
    if "inspect" in results:
        for row in [results["inspect"]["first"], *results["inspect"]["repeated"]]:
            if row.get("advisory_completed") is not True:
                violations.append("Explicit inspection was not confirmed complete; its elapsed time is not a completed-scan measurement")
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--components", type=int, default=58)
    parser.add_argument("--outside-files", type=int, default=512)
    parser.add_argument("--outside-payload-bytes", type=int, default=4096)
    parser.add_argument("--fixture-parent", type=Path,
                        help="Existing scratch directory on the filesystem to measure; never a real checkout")
    parser.add_argument("--storage-label", default="unspecified",
                        help="Operator-supplied storage label, not automatic filesystem detection")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--include-inspect", action="store_true")
    parser.add_argument("--enforce", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 6 <= args.components <= 232 or not 0 <= args.outside_files <= 8000 or not 1 <= args.runs <= 20:
        parser.error("Components 6..232, outside files 0..8000, runs 1..20 required")
    if not 0 <= args.outside_payload_bytes <= 65536 or args.outside_files * (args.outside_payload_bytes + 32) > 256 * 1024 * 1024:
        parser.error("Outside payload must be 0..65536 bytes per file, within 256 MiB total")
    if args.fixture_parent is not None and not args.fixture_parent.is_dir():
        parser.error("Fixture parent must be an existing scratch directory")
    if not 0 < len(args.storage_label) <= 64 or not all(32 <= ord(char) <= 126 for char in args.storage_label):
        parser.error("Storage label must be 1..64 printable ASCII characters")
    with tempfile.TemporaryDirectory(prefix="boundver-cli-profile-", dir=args.fixture_parent) as directory:
        result = profile(Path(directory), args.components, args.outside_files, args.runs, args.include_inspect,
                         args.outside_payload_bytes, args.storage_label)
    violations = contract_violations(result)
    result["performance_contract"] = {
        "enforced": args.enforce, "passed": not violations, "violations": violations,
        "default_working_tree_reads": 0, "default_advisory_calls": 0,
        "default_write_tree_commands": 0,
        "default_git_process_limit": 20 + 2 * (args.components // 4),
        "default_median_limit_seconds": 1.5 * result["results"]["quiet"]["median_wall_seconds"] + 0.25,
    }
    rendered = json.dumps(result, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 1 if args.enforce and violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
