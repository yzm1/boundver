#!/usr/bin/env python3
"""Reproducible intrinsic-runtime benchmark for boundver verification.

The fixture deliberately uses real Git, 20 components, six provider families,
version files, behavior declarations, consumer edges, and slices. Results name
the host, distinguish first/repeated runs, and attribute both wall/CPU time and
Git process starts to each operation phase.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Dict, Iterator, List, Tuple
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from boundver._config import (  # noqa: E402
    find_config_file,
    load_config_file,
    validate_config,
)
from boundver import _git as git_helpers  # noqa: E402
from boundver import _lockfile as lockfile_helpers  # noqa: E402
from boundver._git import (  # noqa: E402
    _capture_git_source_snapshot,
)
from boundver._lockfile import (  # noqa: E402
    dump_lockfile,
    generate_lockfile,
    load_lockfile_file,
    verify_lockfile,
)
from boundver._utils import _bounded_json_dumps  # noqa: E402
from boundver.core import _facet_policy_payload  # noqa: E402


COMPONENT_COUNT = 20
FILES_PER_COMPONENT = 8
PERFORMANCE_CONTRACT = {
    "hardware": "GitHub Actions ubuntu-latest, Python 3.12",
    "head": {
        "first_wall_seconds": 5.0,
        "repeated_median_wall_seconds": 3.0,
        "git_processes": 6,
    },
    "small_staged_change": {
        "first_wall_seconds": 5.0,
        "repeated_median_wall_seconds": 3.0,
        # Index capture proves both its tree object and path membership stable,
        # then the three readers open independent bounded blob sessions.
        "git_processes": 10,
    },
}


def _run(root: Path, *args: str) -> str:
    """Run one trusted fixture command outside the production read-only API."""
    command = [
        git_helpers._trusted_git_executable(root),
        "-C",
        str(root.resolve(strict=True)),
        *args,
    ]
    result = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=git_helpers._git_subprocess_env(),
        timeout=git_helpers.MAX_GIT_COMMAND_SECONDS,
        check=False,
    )
    if len(result.stdout) > git_helpers.MAX_GIT_COMMAND_OUTPUT_BYTES:
        raise RuntimeError("Git output exceeds the runtime benchmark limit")
    if len(result.stderr) > git_helpers.MAX_GIT_DIAGNOSTIC_BYTES:
        raise RuntimeError("Git diagnostic exceeds the runtime benchmark limit")
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            command,
            output=result.stdout,
            stderr=result.stderr,
        )
    return result.stdout.decode("utf-8", errors="replace").strip()


def _init_repository(root: Path) -> None:
    """Initialize the trusted benchmark fixture before a worktree exists."""
    _run(root, "init", "-b", "main")


def _provider_declaration(index: int) -> Tuple[dict, List[str]]:
    kind = index % 6
    if kind == 0:
        return {"provider": "path-hash", "paths": ["contract.txt"]}, [
            "contract.txt"
        ]
    if kind == 1:
        return {"provider": "json-canonical", "paths": ["contract.json"]}, [
            "contract.json"
        ]
    if kind == 2:
        return {
            "provider": "openapi-canonical",
            "paths": ["openapi.json"],
        }, ["openapi.json"]
    if kind == 3:
        return {"provider": "python-exports", "paths": ["__init__.py"]}, [
            "__init__.py"
        ]
    if kind == 4:
        return {"provider": "typescript-exports", "paths": ["index.ts"]}, [
            "index.ts"
        ]
    return {"provider": "leaf", "paths": []}, []


def _write_fixture(root: Path) -> dict:
    components: Dict[str, dict] = {}
    for index in range(COMPONENT_COUNT):
        name = f"component-{index:02d}"
        component = root / "components" / name
        component.mkdir(parents=True)
        for file_index in range(FILES_PER_COMPONENT):
            (component / f"impl-{file_index}.txt").write_text(
                f"{name} implementation {file_index}\n",
                encoding="utf-8",
            )
        (component / "contract.txt").write_text(
            f"contract {index}\n", encoding="utf-8"
        )
        (component / "contract.json").write_text(
            json.dumps({"component": name, "version": index}) + "\n",
            encoding="utf-8",
        )
        (component / "openapi.json").write_text(
            json.dumps(
                {
                    "openapi": "3.0.0",
                    "info": {"title": name, "version": "1.0.0"},
                    "paths": {f"/{name}": {"get": {"responses": {"200": {"description": "ok"}}}}},
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (component / "__init__.py").write_text(
            f"PUBLIC_{index} = {index}\n", encoding="utf-8"
        )
        (component / "index.ts").write_text(
            f"export const public{index} = {index};\n", encoding="utf-8"
        )
        (component / "package.json").write_text(
            json.dumps({"name": name, "version": f"1.{index}.0"}) + "\n",
            encoding="utf-8",
        )

        boundary, boundary_paths = _provider_declaration(index)
        consumers = [f"component-{index + 1:02d}"] if index + 1 < COMPONENT_COUNT else []
        facets = ["exact"]
        if boundary_paths:
            facets.extend(["behavior", "boundary"])
        if index % 2 == 0:
            facets.append("compat")
        entry: dict = {
            "path": f"components/{name}",
            "boundary": boundary,
            "consumers": consumers,
            "verify_facets": facets,
        }
        if boundary_paths:
            entry["behavior"] = {"paths": boundary_paths + ["package.json"]}
        if index % 2 == 0:
            entry["version_source"] = {
                "file": "package.json",
                "field": "version",
            }
        components[name] = entry

    config = {
        "project": "boundver-runtime-benchmark",
        "components": components,
        "slices": {
            "all-components": {
                "mode": "exact",
                "components": sorted(components),
            },
            "downstream-chain": {
                "mode": "exact",
                "closure_of": "component-00",
            },
        },
    }
    (root / "boundary.config.json").write_text(
        json.dumps(config, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return config


def create_fixture(root: Path) -> None:
    _init_repository(root)
    _run(root, "config", "user.email", "benchmark@example.invalid")
    _run(root, "config", "user.name", "Boundver Benchmark")
    config = _write_fixture(root)
    _run(root, "add", "--all")
    snapshot = _capture_git_source_snapshot(root, "index")
    errors = validate_config(config, root, source="index", snapshot=snapshot)
    if errors:
        raise RuntimeError("benchmark fixture config is invalid:\n" + "\n".join(errors))
    lockfile = generate_lockfile(config, root, source="index", snapshot=snapshot)
    (root / "boundary.lock.json").write_text(
        dump_lockfile(lockfile), encoding="utf-8"
    )
    _run(root, "add", "--all")
    _run(root, "commit", "-m", "benchmark fixture")


def _git_command_name(command: object) -> str:
    if not isinstance(command, (list, tuple)) or not command:
        return "non-git"
    values = [str(value) for value in command]
    if Path(values[0]).name.lower() not in {"git", "git.exe"}:
        return "non-git"
    try:
        root_flag = values.index("-C")
    except ValueError:
        start = 1
    else:
        start = root_flag + 2
    while start < len(values):
        value = values[start]
        if value.startswith(("--work-tree=", "--git-dir=")) or value in {
            "--literal-pathspecs",
            "--no-pager",
        }:
            start += 1
            continue
        if value == "-c" and start + 1 < len(values):
            start += 2
            continue
        break
    return values[start] if start < len(values) else "git"


@contextmanager
def _count_git_processes() -> Iterator[Counter]:
    original = subprocess.Popen
    counts: Counter = Counter()

    def counted(*args: Any, **kwargs: Any) -> subprocess.Popen:
        command = args[0] if args else kwargs.get("args")
        name = _git_command_name(command)
        if name != "non-git":
            counts[name] += 1
        return original(*args, **kwargs)

    with patch.object(subprocess, "Popen", counted):
        yield counts


def _measure(name: str, callback: Callable[[], Any]) -> Tuple[Any, dict]:
    with _count_git_processes() as process_counts:
        cpu_start = time.process_time()
        wall_start = time.perf_counter()
        value = callback()
        wall_seconds = time.perf_counter() - wall_start
        cpu_seconds = time.process_time() - cpu_start
    return value, {
        "name": name,
        "wall_seconds": round(wall_seconds, 6),
        "cpu_seconds": round(cpu_seconds, 6),
        "git_processes": sum(process_counts.values()),
        "git_commands": dict(sorted(process_counts.items())),
    }


@contextmanager
def _profile_source_work() -> Iterator[dict]:
    """Count and time inclusive fingerprint work without changing production."""
    metrics = {
        name: {"calls": 0, "bytes": 0, "wall_seconds": 0.0}
        for name in (
            "git_blob_reads",
            "source_file_reads",
            "exact_tree_hashes",
            "provider_extractions",
        )
    }
    original_blob_read = git_helpers._GitBlobSession.read_blob
    original_file_read = lockfile_helpers._SourceAccessor.read_file_limited
    original_tree_digest = lockfile_helpers.source_tree_digest
    original_boundary = lockfile_helpers.compute_boundary

    def measured(name: str, callback: Callable[[], Any]) -> Any:
        started = time.perf_counter()
        try:
            result = callback()
        finally:
            metrics[name]["calls"] += 1
            metrics[name]["wall_seconds"] += time.perf_counter() - started
        if isinstance(result, (bytes, bytearray)):
            metrics[name]["bytes"] += len(result)
        return result

    def read_blob(self: Any, *args: Any, **kwargs: Any) -> bytes:
        return measured(
            "git_blob_reads",
            lambda: original_blob_read(self, *args, **kwargs),
        )

    def read_file(self: Any, *args: Any, **kwargs: Any) -> bytes:
        return measured(
            "source_file_reads",
            lambda: original_file_read(self, *args, **kwargs),
        )

    def tree_digest(*args: Any, **kwargs: Any) -> Any:
        return measured(
            "exact_tree_hashes",
            lambda: original_tree_digest(*args, **kwargs),
        )

    def boundary(*args: Any, **kwargs: Any) -> Any:
        return measured(
            "provider_extractions",
            lambda: original_boundary(*args, **kwargs),
        )

    with (
        patch.object(git_helpers._GitBlobSession, "read_blob", read_blob),
        patch.object(
            lockfile_helpers._SourceAccessor,
            "read_file_limited",
            read_file,
        ),
        patch.object(lockfile_helpers, "source_tree_digest", tree_digest),
        patch.object(lockfile_helpers, "compute_boundary", boundary),
    ):
        yield metrics
    for metric in metrics.values():
        metric["wall_seconds"] = round(metric["wall_seconds"], 6)


def measure_verify(root: Path, source: str) -> dict:
    phases: List[dict] = []

    snapshot, metric = _measure(
        "capture_snapshot",
        lambda: _capture_git_source_snapshot(root, source),
    )
    phases.append(metric)
    config_path = find_config_file(root, snapshot=snapshot)
    config, metric = _measure(
        "load_config",
        lambda: load_config_file(config_path, repo_root=root, snapshot=snapshot),
    )
    phases.append(metric)
    errors, metric = _measure(
        "validate_config",
        lambda: validate_config(config, root, source=source, snapshot=snapshot),
    )
    phases.append(metric)
    if errors:
        raise RuntimeError("benchmark config validation failed:\n" + "\n".join(errors))
    lockfile, metric = _measure(
        "load_lock",
        lambda: load_lockfile_file(
            root / "boundary.lock.json",
            repo_root=root,
            snapshot=snapshot,
        ),
    )
    phases.append(metric)
    observations: List[str] = []
    consumer_impact: List[dict] = []
    with _profile_source_work() as source_work:
        issues, metric = _measure(
            "verify_fingerprints",
            lambda: verify_lockfile(
                config,
                lockfile,
                root,
                source=source,
                observations=observations,
                consumer_impact=consumer_impact,
                snapshot=snapshot,
            ),
        )
    metric["source_work"] = source_work
    phases.append(metric)
    payload = {
        "issues": issues,
        "observations": observations,
        "consumer_impact": consumer_impact,
        "facet_policy": _facet_policy_payload(config, None),
    }
    _rendered, metric = _measure(
        "render_json",
        lambda: _bounded_json_dumps(payload, indent=2, sort_keys=True),
    )
    phases.append(metric)
    return {
        "source": source,
        "ok": not issues,
        "issue_count": len(issues),
        "observation_count": len(observations),
        "wall_seconds": round(sum(item["wall_seconds"] for item in phases), 6),
        "cpu_seconds": round(sum(item["cpu_seconds"] for item in phases), 6),
        "git_processes": sum(item["git_processes"] for item in phases),
        "phases": phases,
    }


def _summary(runs: List[dict]) -> dict:
    return {
        "runs": runs,
        "median_wall_seconds": round(
            statistics.median(run["wall_seconds"] for run in runs), 6
        ),
        "max_git_processes": max(run["git_processes"] for run in runs),
    }


def run_benchmark(root: Path, repeated_runs: int) -> dict:
    create_fixture(root)
    first_head = measure_verify(root, "head")
    repeated_head = [measure_verify(root, "head") for _ in range(repeated_runs)]

    changed = root / "components" / "component-00" / "impl-0.txt"
    changed.write_text("small staged implementation change\n", encoding="utf-8")
    _run(root, "add", changed.relative_to(root).as_posix())
    first_index = measure_verify(root, "index")
    repeated_index = [measure_verify(root, "index") for _ in range(repeated_runs)]

    return {
        "schema_version": 1,
        "fixture": {
            "components": COMPONENT_COUNT,
            "files_per_component": FILES_PER_COMPONENT,
            "providers": [
                "path-hash",
                "json-canonical",
                "openapi-canonical",
                "python-exports",
                "typescript-exports",
                "leaf",
            ],
        },
        "host": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "processor": platform.processor(),
            "cpu_count": os.cpu_count(),
            "git": _run(root, "--version"),
            "github_runner": os.environ.get("RUNNER_NAME"),
            "github_runner_os": os.environ.get("RUNNER_OS"),
            "github_runner_arch": os.environ.get("RUNNER_ARCH"),
            "github_image_os": os.environ.get("ImageOS"),
            "github_image_version": os.environ.get("ImageVersion"),
        },
        "head": {
            "first": first_head,
            "repeated": _summary(repeated_head),
        },
        "small_staged_change": {
            "first": first_index,
            "repeated": _summary(repeated_index),
        },
    }


def _contract_violations(result: dict) -> List[str]:
    violations: List[str] = []
    for case_name in ("head", "small_staged_change"):
        case = result[case_name]
        target = PERFORMANCE_CONTRACT[case_name]
        first = case["first"]
        repeated = case["repeated"]
        if first["git_processes"] > target["git_processes"]:
            violations.append(
                f"{case_name} first run started {first['git_processes']} Git "
                f"processes; limit is {target['git_processes']}"
            )
        if repeated["max_git_processes"] > target["git_processes"]:
            violations.append(
                f"{case_name} repeated run started "
                f"{repeated['max_git_processes']} Git processes; limit is "
                f"{target['git_processes']}"
            )
        if first["wall_seconds"] > target["first_wall_seconds"]:
            violations.append(
                f"{case_name} first run took {first['wall_seconds']}s; limit is "
                f"{target['first_wall_seconds']}s"
            )
        if (
            repeated["median_wall_seconds"]
            > target["repeated_median_wall_seconds"]
        ):
            violations.append(
                f"{case_name} repeated median took "
                f"{repeated['median_wall_seconds']}s; limit is "
                f"{target['repeated_median_wall_seconds']}s"
            )
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3, help="Repeated warm runs")
    parser.add_argument("--output", type=Path, help="Optional JSON result path")
    parser.add_argument(
        "--enforce",
        action="store_true",
        help="Fail when the committed CI performance contract is exceeded",
    )
    args = parser.parse_args()
    if args.runs < 1 or args.runs > 20:
        parser.error("--runs must be between 1 and 20")

    with tempfile.TemporaryDirectory(prefix="boundver-runtime-benchmark-") as td:
        result = run_benchmark(Path(td), args.runs)
    violations = _contract_violations(result)
    result["performance_contract"] = {
        **PERFORMANCE_CONTRACT,
        "enforced": args.enforce,
        "passed": not violations,
        "violations": violations,
    }
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 1 if args.enforce and violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
