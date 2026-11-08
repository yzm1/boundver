#!/usr/bin/env python3
"""Local 58-component adoption profile; no network or hosted CI dispatch.

This is a synthetic workload, not an HSL benchmark. First runs do not imply
flushed OS caches. Measurements include instrumentation overhead.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import statistics
import sys
import tempfile

# Isolated script execution deliberately omits cwd from sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import benchmark_runtime as runtime  # noqa: E402
from boundver._config import config_warnings  # noqa: E402
from boundver._derivations import (  # noqa: E402
    build_derivation_evidence,
    dump_derivation_evidence,
    verify_derivations,
)


def create_fixture(root: Path, count: int = 58) -> None:
    runtime._init_repository(root)
    runtime._run(root, "config", "user.email", "benchmark@example.invalid")
    runtime._run(root, "config", "user.name", "Boundver Benchmark")
    config = runtime._write_fixture(root, count)
    for index, component in enumerate(config["components"].values()):
        mode = index % 4
        if mode == 1:
            component["version_source"] = {"component": "component-00"}
        elif mode == 2:
            component["version_source"] = {"constant": "1.2.0"}
        elif mode == 3:
            component["version_source"] = {"git_tag_prefix": f"job-{index:02d}-v"}
    # Shared source feeds multiple independent receipts and published outputs.
    (root / "generator-input.json").write_text('{"generator":1}\n', encoding="utf-8")
    config["derivations"] = {
        f"api-{index:02d}": {
            "inputs": ["generator-input.json"],
            "outputs": [f"components/component-{index:02d}/openapi.json"],
            "evidence": f"receipt-{index:02d}.boundver-derivation.json",
            "generator": "synthetic-openapi/v1",
        }
        for index in range(2, count, 6)
    }
    (root / "boundary.config.json").write_text(
        json.dumps(config, sort_keys=True, indent=2) + "\n", encoding="utf-8",
    )
    runtime._run(root, "add", "--all")
    runtime._run(root, "commit", "-m", "adoption source fixture")
    for index in range(3, count, 4):
        runtime._run(root, "tag", f"job-{index:02d}-v1.0.0")
    with runtime.lockfile_helpers._SourceAccessor(root, "head") as accessor:
        for name in config["derivations"]:
            path, evidence = build_derivation_evidence(config, name, accessor)
            (root / path).write_text(dump_derivation_evidence(evidence), encoding="utf-8")
    runtime._run(root, "add", "--all")
    runtime._run(root, "commit", "-m", "adoption derivation receipts")
    snapshot = runtime._capture_git_source_snapshot(root, "head")
    errors = runtime.validate_config(config, root, source="head", snapshot=snapshot)
    if errors:
        raise RuntimeError("Invalid adoption fixture: " + "; ".join(errors))
    lock = runtime.generate_lockfile(config, root, source="head", snapshot=snapshot)
    (root / "boundary.lock.json").write_text(runtime.dump_lockfile(lock), encoding="utf-8")
    runtime._run(root, "add", "--all")
    runtime._run(root, "commit", "-m", "adoption fixture lock")


def measure(root: Path, operation: str) -> dict:
    """Measure independent operations; do not reuse another command's capture."""
    if operation == "verify":
        return runtime.measure_verify(root, "head")
    phases = []
    snapshot, metric = runtime._measure(
        "capture_snapshot", lambda: runtime._capture_git_source_snapshot(root, "head"),
    )
    phases.append(metric)
    config, metric = runtime._measure(
        "load_config", lambda: runtime.load_config_file(
            root / "boundary.config.json", repo_root=root, snapshot=snapshot,
        ),
    )
    phases.append(metric)
    errors, metric = runtime._measure(
        "validate_config", lambda: runtime.validate_config(
            config, root, source="head", snapshot=snapshot,
        ),
    )
    phases.append(metric)
    if errors:
        raise RuntimeError("Validation failed: " + "; ".join(errors))
    if operation == "validate-config":
        def freshness():
            with runtime.lockfile_helpers._SourceAccessor(
                root, "head", snapshot=snapshot,
            ) as accessor:
                return verify_derivations(config, accessor)
        errors, metric = runtime._measure("validate_derivations", freshness)
        phases.append(metric)
        if errors:
            raise RuntimeError("Freshness failed: " + "; ".join(errors))
        _, metric = runtime._measure(
            "config_warnings", lambda: config_warnings(
                config, root, source="head", snapshot=snapshot,
            ),
        )
        phases.append(metric)
    if operation == "generate":
        with runtime._profile_source_work() as work:
            lock, metric = runtime._measure(
                "generate_fingerprints", lambda: runtime.generate_lockfile(
                    config, root, source="head", snapshot=snapshot,
                ),
            )
        metric["source_work"] = work
        phases.append(metric)
        _, metric = runtime._measure("serialize_lock", lambda: runtime.dump_lockfile(lock))
        phases.append(metric)
    elif operation != "validate-config":
        raise ValueError(f"Unknown operation: {operation}")
    return {
        "source": "head", "ok": True,
        "wall_seconds": round(sum(row["wall_seconds"] for row in phases), 6),
        "cpu_seconds": round(sum(row["cpu_seconds"] for row in phases), 6),
        "git_processes": sum(row["git_processes"] for row in phases),
        "phases": phases,
    }


def benchmark(root: Path, count: int, runs: int) -> dict:
    create_fixture(root, count)
    operations = {}
    for operation in ("validate-config", "verify", "generate"):
        first = measure(root, operation)
        repeated = [measure(root, operation) for _ in range(runs)]
        wall = [row["wall_seconds"] for row in repeated]
        operations[operation] = {
            "first": first, "repeated": repeated,
            "median_wall_seconds": statistics.median(wall),
            "min_wall_seconds": min(wall), "max_wall_seconds": max(wall),
            "population_stdev_wall_seconds": statistics.pstdev(wall),
        }
    return {
        "schema_version": 1,
        "fixture": {"components": count, "synthetic": True,
                    "features": ["six-provider-families", "file-versions", "tag-versions",
                                 "inherited-versions", "constant-versions", "consumer-chain",
                                 "slices", "derived-artifacts", "shared-generator-input"]},
        "host": {"platform": platform.platform(), "python": platform.python_version(),
                 "git": runtime._run(root, "--version")},
        "measurement": "In-process API phases; excludes CLI startup, atomic file publication, and OS cache flushing",
        "operations": operations,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--components", type=int, default=58)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 6 <= args.components <= 232 or not 1 <= args.runs <= 20:
        parser.error("--components must be 6..232 and --runs 1..20")
    with tempfile.TemporaryDirectory(prefix="boundver-adoption-") as directory:
        result = benchmark(Path(directory), args.components, args.runs)
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
