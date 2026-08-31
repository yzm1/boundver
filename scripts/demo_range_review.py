#!/usr/bin/env python3
"""Run the public 17-component historical range-review demonstration."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Sequence, Tuple


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "examples" / "range-review" / "fixture"
CASE_STUDY = ROOT / "docs" / "case-study-range-review.md"
LOCK_SCHEMA_URL = (
    "https://raw.githubusercontent.com/yzm1/boundver/"
    "v0.13.0/spec/boundary.lock.schema.json"
)
EXPECTED_FACETS = {
    "admin-portal": ["exact"],
    "analytics-api": ["exact", "behavior"],
    "gateway-api": ["exact", "behavior", "boundary"],
}
EXPECTED_DIRECT_COMPONENTS = ["analytics-contracts", "platform-client"]
EXPECTED_TRANSITIVE_COMPONENTS = [
    "admin-portal",
    "analytics-contracts",
    "checkout-web",
    "insights-web",
    "platform-client",
    "scheduler",
]
EXPECTED_DIRECT_EXTERNAL = ["partner-audit"]
EXPECTED_TRANSITIVE_EXTERNAL = ["mobile-app", "partner-audit"]
EXPECTED_CHANGED_SLICES = [
    "analytics-behavior",
    "frontend-impact",
    "gateway-contract",
    "release-surface",
    "shared-runtime-impact",
]
EXPECTED_STRUCTURAL_PATH = "/paths/~1orders~1{id}"
EXPECTED_BASE_OID = "72dc308d53b356b190e97d8309ee637565499b27"
EXPECTED_TARGET_OID = "70383483c18a1dc57962402a96d0b14a8728c690"
EXPECTED_PROVIDER_FAMILIES = {
    "json-canonical",
    "leaf",
    "openapi-canonical",
    "path-hash",
    "python-exports",
    "typescript-exports",
}
OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class DemoError(RuntimeError):
    """The public demo no longer matches its documented contract."""


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Dict[str, str],
) -> Tuple[subprocess.CompletedProcess[str], float]:
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed, time.perf_counter() - started


def _require_code(
    completed: subprocess.CompletedProcess[str],
    expected: int,
    label: str,
) -> None:
    if completed.returncode == expected:
        return
    detail = "\n".join(
        value.strip() for value in (completed.stdout, completed.stderr) if value.strip()
    )
    raise DemoError(
        f"{label} returned {completed.returncode}, expected {expected}"
        + (f":\n{detail}" if detail else "")
    )


def _json_result(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Dict[str, str],
    expected_code: int,
    label: str,
) -> Tuple[dict, float]:
    completed, elapsed = _run(command, cwd=cwd, env=env)
    _require_code(completed, expected_code, label)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise DemoError(f"{label} did not emit valid JSON") from exc
    if not isinstance(payload, dict):
        raise DemoError(f"{label} JSON root is not an object")
    return payload, elapsed


def _git(
    repo: Path,
    env: Dict[str, str],
    *arguments: str,
) -> str:
    completed, _elapsed = _run(["git", *arguments], cwd=repo, env=env)
    _require_code(completed, 0, f"git {' '.join(arguments)}")
    return completed.stdout.strip()


def _commit(
    repo: Path,
    env: Dict[str, str],
    message: str,
    timestamp: str,
) -> str:
    commit_env = env.copy()
    commit_env["GIT_AUTHOR_DATE"] = timestamp
    commit_env["GIT_COMMITTER_DATE"] = timestamp
    _git(repo, commit_env, "commit", "--quiet", "-m", message)
    oid = _git(repo, env, "rev-parse", "HEAD")
    if OID_RE.fullmatch(oid) is None:
        raise DemoError(f"git produced an invalid commit identity: {oid!r}")
    return oid


def _write_json(path: Path, value: object) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(
            json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        )


def _normalize_lock(repo: Path) -> None:
    path = repo / "boundary.lock.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["$schema"] = LOCK_SCHEMA_URL
    _write_json(path, payload)


def _apply_changes(repo: Path) -> None:
    admin = repo / "components" / "admin-portal" / "app.txt"
    with admin.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write("feature_flag=true\n")

    defaults_path = repo / "components" / "analytics-api" / "defaults.json"
    defaults = json.loads(defaults_path.read_text(encoding="utf-8"))
    defaults["aggregation_mode"] = "adaptive"
    _write_json(defaults_path, defaults)

    api_path = repo / "components" / "gateway-api" / "api.json"
    api = json.loads(api_path.read_text(encoding="utf-8"))
    api["paths"]["/orders/{id}"] = {
        "get": {"responses": {"200": {}}}
    }
    _write_json(api_path, api)


def _review_facets(payload: dict) -> Dict[str, List[str]]:
    changed = payload.get("components", {}).get("changed", [])
    return {
        component["name"]: [item["facet"] for item in component["facets"]]
        for component in changed
    }


def _verify_facets(payload: dict) -> Dict[str, List[str]]:
    result: Dict[str, List[str]] = {}
    for issue in [*payload.get("issues", []), *payload.get("observations", [])]:
        match = re.match(
            r"^MISMATCH ([A-Za-z0-9][A-Za-z0-9._-]*)\."
            r"(exact|behavior|boundary|compat):",
            issue,
        )
        if match is None:
            continue
        result.setdefault(match.group(1), []).append(match.group(2))
    order = {name: index for index, name in enumerate(
        ("exact", "behavior", "boundary", "compat")
    )}
    return {
        component: sorted(set(facets), key=order.__getitem__)
        for component, facets in sorted(result.items())
    }


def _impact_names(payload: dict) -> Tuple[List[str], List[str]]:
    impacts = payload.get("consumer_impact", [])
    if len(impacts) != 1 or impacts[0].get("component") != "gateway-api":
        raise DemoError(f"unexpected consumer impact records: {impacts!r}")
    impact = impacts[0]

    def names(key: str) -> List[str]:
        values = impact.get(key, [])
        return sorted(
            value["name"] if isinstance(value, dict) else value
            for value in values
        )

    return names("components"), names("external_consumers")


def _assert_provenance(payload: dict, base: str, target: str) -> None:
    endpoints = payload.get("endpoints", {})
    for label, expected in (("base", base), ("target", target)):
        endpoint = endpoints.get(label, {})
        if endpoint.get("requested_ref") != expected:
            raise DemoError(f"{label} requested ref is not bound to {expected}")
        for field in ("requested_commit", "commit"):
            if endpoint.get(field) != expected:
                raise DemoError(f"{label} {field} is not bound to {expected}")
        if OID_RE.fullmatch(str(endpoint.get("tree", ""))) is None:
            raise DemoError(f"{label} tree identity is missing or malformed")
        if endpoint.get("config") != f"{expected}:boundary.config.json":
            raise DemoError(f"{label} config provenance is not exact")
        if endpoint.get("lock") != f"{expected}:boundary.lock.json":
            raise DemoError(f"{label} lock provenance is not exact")


def _assert_structural(payload: dict, base: str, target: str) -> None:
    structural = payload.get("structural_changes", {})
    if structural.get("complete") is not True or structural.get("truncated") is not False:
        raise DemoError("OpenAPI structural explanation is not complete")
    reports = structural.get("reports", [])
    if len(reports) != 1 or reports[0].get("component") != "gateway-api":
        raise DemoError(f"unexpected structural reports: {reports!r}")
    report = reports[0]
    if report.get("claim") != "structural-explanation-only":
        raise DemoError("structural compatibility non-claim is missing")
    inputs = report.get("inputs", {})
    if inputs.get("base", {}).get("commit") != base:
        raise DemoError("structural base input is not commit-bound")
    if inputs.get("target", {}).get("commit") != target:
        raise DemoError("structural target input is not commit-bound")
    paths = [
        change["path"]
        for document in report.get("documents", [])
        for change in document.get("changes", [])
    ]
    if paths != [EXPECTED_STRUCTURAL_PATH]:
        raise DemoError(f"unexpected structural paths: {paths!r}")


def _assert_review(
    direct: dict,
    transitive: dict,
    text: str,
    base: str,
    target: str,
) -> None:
    for payload in (direct, transitive):
        if payload.get("schema") != "boundver-review/v1" or payload.get("complete") is not True:
            raise DemoError("historical review is not a complete v1 result")
        if _review_facets(payload) != EXPECTED_FACETS:
            raise DemoError(f"unexpected review facets: {_review_facets(payload)!r}")
        _assert_provenance(payload, base, target)
        _assert_structural(payload, base, target)

    direct_components, direct_external = _impact_names(direct)
    transitive_components, transitive_external = _impact_names(transitive)
    if direct_components != EXPECTED_DIRECT_COMPONENTS:
        raise DemoError(f"unexpected direct components: {direct_components!r}")
    if direct_external != EXPECTED_DIRECT_EXTERNAL:
        raise DemoError(f"unexpected direct external consumers: {direct_external!r}")
    if transitive_components != EXPECTED_TRANSITIVE_COMPONENTS:
        raise DemoError(f"unexpected transitive components: {transitive_components!r}")
    if transitive_external != EXPECTED_TRANSITIVE_EXTERNAL:
        raise DemoError(
            f"unexpected transitive external consumers: {transitive_external!r}"
        )
    changed_slices = sorted(item["name"] for item in transitive["slices"]["changed"])
    if changed_slices != EXPECTED_CHANGED_SLICES:
        raise DemoError(f"unexpected changed slices: {changed_slices!r}")

    required_text = (
        "CHANGED COMPONENTS (3)",
        "admin-portal [changed]",
        "analytics-api [changed]",
        "gateway-api [changed]",
        "Structural explanation: complete",
        EXPECTED_STRUCTURAL_PATH,
        "not a compatibility verdict",
    )
    missing = [value for value in required_text if value not in text]
    if missing:
        raise DemoError(f"text and JSON review output diverged; missing {missing!r}")


def _summary(
    base: str,
    target: str,
    verify_seconds: float,
    direct_seconds: float,
    transitive_seconds: float,
) -> None:
    print("SANITIZED RANGE-REVIEW DEMO")
    print("Fixture: 17 components, 6 slices")
    print("Before lock reconciliation:")
    for component, facets in EXPECTED_FACETS.items():
        print(f"  {component}: {', '.join(facets)}")
    print("After lock reconciliation: the same three historical transitions remain")
    print("Direct consumers: " + ", ".join(EXPECTED_DIRECT_COMPONENTS))
    print("Transitive consumers: " + ", ".join(EXPECTED_TRANSITIVE_COMPONENTS))
    print("External consumers: " + ", ".join(EXPECTED_TRANSITIVE_EXTERNAL))
    print(f"Structural change: added {EXPECTED_STRUCTURAL_PATH}")
    print(f"Provenance: base={base} target={target}")
    print("Observed timings for this run only (not a benchmark):")
    print(f"  pre-reconciliation verify: {verify_seconds:.3f}s")
    print(f"  direct historical review: {direct_seconds:.3f}s")
    print(f"  transitive historical review: {transitive_seconds:.3f}s")
    print("Demo passed: current drift and reconciled historical review agree.")


def _assert_published_capture() -> None:
    try:
        text = CASE_STUDY.read_text(encoding="utf-8")
    except OSError as exc:
        raise DemoError(f"published case study is unavailable: {CASE_STUDY}") from exc
    required = (
        "Fixture: 17 components, 6 slices",
        "admin-portal: exact",
        "analytics-api: exact, behavior",
        "gateway-api: exact, behavior, boundary",
        EXPECTED_STRUCTURAL_PATH,
        EXPECTED_BASE_OID,
        EXPECTED_TARGET_OID,
        "not a benchmark",
    )
    missing = [value for value in required if value not in text]
    if missing:
        raise DemoError(f"published case-study capture drifted; missing {missing!r}")


def main() -> int:
    if not FIXTURE.is_dir():
        print(f"demo fixture is missing: {FIXTURE}", file=sys.stderr)
        return 1
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    boundver = [sys.executable, "-m", "boundver"]

    try:
        _assert_published_capture()
        with tempfile.TemporaryDirectory(prefix="boundver-range-demo-") as temporary:
            repo = Path(temporary)
            shutil.copytree(FIXTURE, repo, dirs_exist_ok=True)
            config = json.loads(
                (repo / "boundary.config.json").read_text(encoding="utf-8")
            )
            if len(config.get("components", {})) != 17:
                raise DemoError("fixture must contain exactly 17 components")
            if len(config.get("slices", {})) != 6:
                raise DemoError("fixture must contain exactly six slices")
            providers = {
                component["boundary"]["provider"]
                for component in config["components"].values()
            }
            if providers != EXPECTED_PROVIDER_FAMILIES:
                raise DemoError(
                    "fixture must contain the six expected provider families: "
                    f"{sorted(providers)!r}"
                )

            for arguments in (
                (
                    "init",
                    "--quiet",
                    "--initial-branch=main",
                    "--object-format=sha1",
                ),
                ("config", "user.email", "demo@boundver.invalid"),
                ("config", "user.name", "boundver range demo"),
                ("config", "core.autocrlf", "false"),
                ("config", "core.filemode", "false"),
                ("config", "core.hooksPath", ".git/no-hooks"),
                ("config", "commit.gpgsign", "false"),
                ("add", "--force", "."),
            ):
                _git(repo, environment, *arguments)

            generated, _elapsed = _run(
                [*boundver, "generate", "--source", "index"],
                cwd=repo,
                env=environment,
            )
            _require_code(generated, 0, "baseline lock generation")
            _normalize_lock(repo)
            _git(repo, environment, "add", "boundary.lock.json")
            base = _commit(
                repo,
                environment,
                "baseline contracts",
                "2026-01-01T00:00:00+00:00",
            )
            if base != EXPECTED_BASE_OID:
                raise DemoError(
                    f"baseline fixture identity drifted: {base} != {EXPECTED_BASE_OID}"
                )

            _apply_changes(repo)
            _git(repo, environment, "add", "--force", "components")
            before, verify_seconds = _json_result(
                [
                    *boundver,
                    "verify",
                    "--source",
                    "index",
                    "--transitive",
                    "--format",
                    "json",
                ],
                cwd=repo,
                env=environment,
                expected_code=4,
                label="pre-reconciliation verification",
            )
            if _verify_facets(before) != EXPECTED_FACETS:
                raise DemoError(
                    f"unexpected pre-reconciliation facets: {_verify_facets(before)!r}"
                )
            before_components, before_external = _impact_names(before)
            if before_components != EXPECTED_TRANSITIVE_COMPONENTS:
                raise DemoError(
                    f"unexpected pre-reconciliation consumers: {before_components!r}"
                )
            if before_external != EXPECTED_TRANSITIVE_EXTERNAL:
                raise DemoError(
                    "unexpected pre-reconciliation external consumers: "
                    f"{before_external!r}"
                )

            generated, _elapsed = _run(
                [*boundver, "generate", "--source", "index"],
                cwd=repo,
                env=environment,
            )
            _require_code(generated, 0, "target lock generation")
            _normalize_lock(repo)
            _git(repo, environment, "add", "boundary.lock.json")
            target = _commit(
                repo,
                environment,
                "reconcile reviewed contracts",
                "2026-01-02T00:00:00+00:00",
            )
            if target != EXPECTED_TARGET_OID:
                raise DemoError(
                    f"target fixture identity drifted: {target} != {EXPECTED_TARGET_OID}"
                )

            clean, _elapsed = _json_result(
                [*boundver, "verify", "--source", "head", "--format", "json"],
                cwd=repo,
                env=environment,
                expected_code=0,
                label="post-reconciliation verification",
            )
            if clean.get("ok") is not True or clean.get("changed_components") != []:
                raise DemoError("reconciled target does not verify cleanly")

            direct, direct_seconds = _json_result(
                [*boundver, "review", f"{base}..{target}", "--format", "json"],
                cwd=repo,
                env=environment,
                expected_code=0,
                label="direct historical review",
            )
            transitive, transitive_seconds = _json_result(
                [
                    *boundver,
                    "review",
                    f"{base}..{target}",
                    "--transitive",
                    "--format",
                    "json",
                ],
                cwd=repo,
                env=environment,
                expected_code=0,
                label="transitive historical review JSON",
            )
            text_result, _elapsed = _run(
                [*boundver, "review", f"{base}..{target}", "--transitive"],
                cwd=repo,
                env=environment,
            )
            _require_code(text_result, 0, "transitive historical review text")
            _assert_review(direct, transitive, text_result.stdout, base, target)
            _summary(
                base,
                target,
                verify_seconds,
                direct_seconds,
                transitive_seconds,
            )
    except (DemoError, OSError, ValueError) as exc:
        print(f"range-review demo failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
