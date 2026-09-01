#!/usr/bin/env python3
"""Run the public consumer-impact demo in a disposable Git repository."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "consumer-impact"
EXPECTED_HUMAN_IMPACT = (
    "Consumer impact:",
    "payments-api [boundary; transitive]",
    "Components: checkout-web, payments-sdk",
    "External consumers: mobile-app",
)
EXPECTED_CONSUMER_IMPACT = [
    {
        "component": "payments-api",
        "facets": ["boundary"],
        "components": ["checkout-web", "payments-sdk"],
        "external_consumers": ["mobile-app"],
        "transitive": True,
    }
]
MAX_DEMO_COMMAND_SECONDS = 300


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    print(f"$ {' '.join(command)}", flush=True)
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            check=False,
            capture_output=capture,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=MAX_DEMO_COMMAND_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "demo command exceeded the "
            f"{MAX_DEMO_COMMAND_SECONDS}-second wall-clock limit"
        ) from exc


def _require_success(result: subprocess.CompletedProcess[str], label: str) -> None:
    if result.returncode != 0:
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        raise RuntimeError(f"{label} failed with exit code {result.returncode}")


def _standalone_config(root: Path) -> None:
    path = root / "boundary.config.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    prefix = "examples/consumer-impact/"
    for component in payload["components"].values():
        component_path = component["path"]
        if not component_path.startswith(prefix):
            raise RuntimeError(f"unexpected example component path: {component_path}")
        component["path"] = component_path.removeprefix(prefix)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    if not EXAMPLE.is_dir():
        print(f"demo fixture is missing: {EXAMPLE}", file=sys.stderr)
        return 1
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    boundver = [sys.executable, "-m", "boundver"]

    try:
        with tempfile.TemporaryDirectory(prefix="boundver-demo-") as temporary:
            demo = Path(temporary)
            shutil.copytree(EXAMPLE, demo, dirs_exist_ok=True)
            lock = demo / "expected.boundary.lock.json"
            lock.unlink(missing_ok=True)
            _standalone_config(demo)

            for command in (
                ["git", "init", "--quiet", "--initial-branch=main"],
                ["git", "config", "user.email", "demo@boundver.invalid"],
                ["git", "config", "user.name", "boundver demo"],
                ["git", "config", "core.autocrlf", "false"],
                ["git", "config", "core.hooksPath", ".git/no-hooks"],
                ["git", "config", "commit.gpgsign", "false"],
                ["git", "add", "--force", "."],
                ["git", "commit", "--quiet", "-m", "baseline contracts"],
            ):
                _require_success(_run(command, cwd=demo), command[0])

            print(f"\nDisposable repository: {demo}\n")
            _require_success(
                _run(
                    [
                        *boundver,
                        "generate",
                        "--source",
                        "working-tree",
                        "--out",
                        "boundary.lock.json",
                    ],
                    cwd=demo,
                    env=env,
                ),
                "baseline generation",
            )

            api = demo / "services" / "payments" / "openapi.yaml"
            with api.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(
                    "  /payments/{id}:\n"
                    "    get:\n"
                    "      operationId: getPayment\n"
                    "      responses:\n"
                    '        "200":\n'
                    "          description: Payment details\n"
                )
            print("\nChanged services/payments/openapi.yaml\n")

            result = _run(
                [
                    *boundver,
                    "verify",
                    "--source",
                    "working-tree",
                    "--lock",
                    "boundary.lock.json",
                    "--transitive",
                ],
                cwd=demo,
                env=env,
                capture=True,
            )
            if result.stdout:
                print(result.stdout, end="")
            if result.stderr:
                print(result.stderr, end="", file=sys.stderr)
            combined = result.stdout + result.stderr
            if result.returncode != 4:
                raise RuntimeError(
                    "expected boundary-drift exit code 4, got "
                    f"{result.returncode}"
                )
            if any(marker not in combined for marker in EXPECTED_HUMAN_IMPACT):
                raise RuntimeError("expected transitive consumer closure was not reported")

            structured = _run(
                [
                    *boundver,
                    "verify",
                    "--source",
                    "working-tree",
                    "--lock",
                    "boundary.lock.json",
                    "--transitive",
                    "--format",
                    "json",
                ],
                cwd=demo,
                env=env,
                capture=True,
            )
            if structured.returncode != 4:
                raise RuntimeError(
                    "expected structured boundary-drift exit code 4, got "
                    f"{structured.returncode}"
                )
            try:
                structured_payload = json.loads(structured.stdout)
            except json.JSONDecodeError as exc:
                raise RuntimeError("structured verification was not valid JSON") from exc
            impact = structured_payload.get("consumer_impact")
            if impact != EXPECTED_CONSUMER_IMPACT:
                raise RuntimeError(
                    f"unexpected structured consumer impact: {impact!r}"
                )
            print("\nStructured consumer_impact:")
            print(json.dumps(impact, indent=2))
    except (OSError, RuntimeError, ValueError) as error:
        print(f"demo failed: {error}", file=sys.stderr)
        return 1

    print(
        "\nDemo passed: boundary drift and the complete human and structured "
        "consumer closure were reported."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
