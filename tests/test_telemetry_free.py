"""Keep the built-in boundver CLI telemetry-free by construction."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from unittest.mock import patch

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.9
    import tomli as tomllib


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = REPO_ROOT / "src" / "boundver"
DISCUSSION_URL = "https://github.com/yzm1/boundver/discussions/100"
VERIFICATION_TOKEN = "kqtqMt_EmnSdRDPAEX8km6LfDuTcOxXnbBWQUFD8TGs"

# urllib.parse remains allowed: parsing a URL does not transmit anything.
FORBIDDEN_IMPORT_PREFIXES = (
    "aiohttp",
    "amplitude",
    "ftplib",
    "grpc",
    "http.client",
    "httpx",
    "mixpanel",
    "opentelemetry",
    "posthog",
    "requests",
    "segment.analytics",
    "sentry_sdk",
    "smtplib",
    "socket",
    "urllib.request",
    "urllib3",
    "websockets",
)
ALLOWED_RUNTIME_DEPENDENCIES = frozenset({"jsonschema", "pyyaml", "tomli"})
FORBIDDEN_PROCESS_CALLS = frozenset(
    {
        "os.popen",
        "os.system",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.run",
    }
)


def _qualified_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _qualified_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _forbidden_import(name: str) -> bool:
    return any(
        name == prefix or name.startswith(prefix + ".")
        for prefix in FORBIDDEN_IMPORT_PREFIXES
    )


def _import_names(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.extend(
                node.module
                if alias.name == "*"
                else f"{node.module}.{alias.name}"
                for alias in node.names
            )
    return names


def _literal_dynamic_import_names(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        if _qualified_name(node.func) not in {
            "__import__",
            "importlib.import_module",
        }:
            continue
        module = node.args[0]
        if isinstance(module, ast.Constant) and isinstance(module.value, str):
            names.append(module.value)
    return names


def _requirement_name(requirement: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
    assert match is not None, f"cannot identify dependency in {requirement!r}"
    return re.sub(r"[-_.]+", "-", match.group(1)).lower()


def test_runtime_has_no_outbound_network_or_telemetry_imports() -> None:
    violations: list[str] = []
    for path in sorted(RUNTIME_ROOT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for name in _import_names(tree) + _literal_dynamic_import_names(tree):
            if _forbidden_import(name):
                violations.append(f"{path.relative_to(REPO_ROOT)} imports {name}")
    assert not violations, "\n".join(violations)


def test_runtime_processes_are_statically_git_rooted() -> None:
    process_calls: list[tuple[Path, ast.Call]] = []
    git_command_assignments: list[ast.Assign] = []

    for path in sorted(RUNTIME_ROOT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _qualified_name(node.func)
                if (
                    name in FORBIDDEN_PROCESS_CALLS
                    or name == "subprocess.Popen"
                    or name.startswith("os.exec")
                    or name.startswith("os.spawn")
                ):
                    process_calls.append((path, node))
            elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "command"
                for target in node.targets
            ):
                git_command_assignments.append(node)

    assert process_calls, "expected bounded local Git process calls"
    assert all(path.name == "_git.py" for path, _ in process_calls)
    assert all(
        _qualified_name(call.func) == "subprocess.Popen"
        and call.args
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == "command"
        and any(
            keyword.arg == "env"
            and isinstance(keyword.value, ast.Call)
            and _qualified_name(keyword.value.func) == "_offline_git_environment"
            for keyword in call.keywords
        )
        for _, call in process_calls
    )
    assert len(git_command_assignments) == len(process_calls)
    assert all(
        isinstance(assignment.value, ast.Call)
        and _qualified_name(assignment.value.func) == "_offline_git_command"
        for assignment in git_command_assignments
    )

    from boundver import _git as git_helpers

    assert git_helpers._offline_git_command(
        Path("repo"), ["--literal-pathspecs", "ls-files", "-z"]
    ) == [
        "git",
        "--no-pager",
        "-C",
        "repo",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "log.showSignature=false",
        "--literal-pathspecs",
        "ls-files",
        "-z",
    ]
    diff_command = git_helpers._offline_git_command(
        Path("repo"),
        ["--literal-pathspecs", "diff", "--name-status", "a" * 40, "b" * 40],
    )
    assert "--no-ext-diff" in diff_command
    assert "--no-textconv" in diff_command
    pathspec_command = git_helpers._offline_git_command(
        Path("repo"),
        [
            "rev-list",
            "--max-count=1",
            "HEAD",
            "--",
            "%Good.lock",
            "--show-signature",
        ],
    )
    assert pathspec_command[-3:] == [
        "--",
        "%Good.lock",
        "--show-signature",
    ]
    for subcommand in ("fetch", "ls-remote", "push", "remote", "send-pack"):
        try:
            git_helpers._offline_git_command(Path("repo"), [subcommand])
        except ValueError:
            continue
        raise AssertionError(f"network-capable Git subcommand allowed: {subcommand}")
    unsafe_commands = (
        ["status", "--short"],
        ["diff", "HEAD", "--"],
        ["diff", "--no-index", "one", "two"],
        ["cat-file", "--filters", "HEAD:file"],
        ["cat-file", "--batch-command"],
        ["ls-files", "--recurse-submodules"],
        ["log", "HEAD"],
        ["rev-list", "--show-signature", "HEAD"],
        ["rev-list", "--format=%G?", "HEAD"],
    )
    for unsafe_args in unsafe_commands:
        try:
            git_helpers._offline_git_command(Path("repo"), unsafe_args)
        except ValueError:
            continue
        raise AssertionError(f"Git worktree inspection allowed: {unsafe_args}")

    environment = {
        "GIT_EXTERNAL_DIFF": "helper",
        "GIT_EXTERNAL_DIFF_TRUST_EXIT_CODE": "true",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.fsmonitor",
        "GIT_CONFIG_VALUE_0": "helper",
        "GIT_CONFIG_PARAMETERS": "'core.fsmonitor'='helper'",
        "GIT_TRACE": "1",
        "GIT_TRACE2_EVENT": "af_unix:stream:/tmp/trace.sock",
        "KEEP_ME": "yes",
    }
    with patch.dict("boundver._git.os.environ", environment, clear=True):
        sanitized = git_helpers._offline_git_environment()
    assert sanitized == {
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "KEEP_ME": "yes",
    }


def test_runtime_dependency_surface_remains_explicit() -> None:
    with (REPO_ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]

    requirements = list(project.get("dependencies", ()))
    for extra, values in project.get("optional-dependencies", {}).items():
        if extra != "dev":
            requirements.extend(values)

    assert all("@" not in requirement for requirement in requirements)
    names = {_requirement_name(requirement) for requirement in requirements}
    assert names <= ALLOWED_RUNTIME_DEPENDENCIES


def test_public_surfaces_preserve_the_promise_and_voluntary_feedback_path() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    privacy = (REPO_ROOT / "docs" / "privacy.md").read_text(encoding="utf-8")
    index = (REPO_ROOT / "docs" / "index.md").read_text(encoding="utf-8")
    override = (REPO_ROOT / "docs" / "overrides" / "main.html").read_text(
        encoding="utf-8"
    )

    promise = "The built-in boundver CLI is telemetry-free"
    assert promise.lower() in readme.lower()
    assert promise in privacy
    assert promise in index
    assert DISCUSSION_URL in readme
    assert DISCUSSION_URL in privacy
    assert DISCUSSION_URL in index
    assert (
        f'<meta name="google-site-verification" content="{VERIFICATION_TOKEN}">'
        in override
    )
