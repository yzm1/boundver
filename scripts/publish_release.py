#!/usr/bin/env python3
"""Fail-closed maintainer gate and dispatcher for a boundver release.

``check`` is read-only.  ``start`` repeats every check and then performs one
mutation: it dispatches ``create-release-tag.yml``.  ``resume`` validates and
reuses the retained artifacts from one failed publication run before its one
mutation: dispatching ``publish.yml`` in explicit recovery mode.  ``alias`` is
the separately confirmed maintainer handoff that performs the one leased
compatibility-tag update after every prerequisite publisher has succeeded.
Protected workflows own exact tags, Releases, Marketplace, package-index, and
container writes and independently verify the compatibility alias.
"""

from __future__ import annotations

import argparse
import fnmatch
import importlib.util
import json
import locale
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO, Sequence


def _load_release_platform():
    """Load the exact adjacent helper even under isolated Python startup."""
    path = Path(__file__).resolve().with_name("_release_platform.py")
    spec = importlib.util.spec_from_file_location(
        "_boundver_release_platform", path
    )
    if spec is None or spec.loader is None:  # pragma: no cover - import invariant
        raise RuntimeError(f"cannot load release platform helper: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


resolve_bash = _load_release_platform().resolve_bash


def _load_release_workflow():
    """Load the exact adjacent workflow helper under isolated startup."""
    path = Path(__file__).resolve().with_name("release_workflow.py")
    name = "_boundver_release_workflow"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - import invariant
        raise RuntimeError(f"cannot load release workflow helper: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


release_workflow = _load_release_workflow()
ReleaseWorkflowError = release_workflow.ReleaseWorkflowError

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.9/3.10
    import tomli as tomllib


REPOSITORY = "yzm1/boundver"
HOMEBREW_REPOSITORY = "yzm1/homebrew-boundver"
REVIEW_TOKEN_ENV = "BOUNDVER_RELEASE_REVIEW_TOKEN"
TAG_RE = re.compile(
    r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
)
SHA_RE = re.compile(r"[0-9a-f]{40}")
ALIAS_RE = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
RUN_ID_RE = re.compile(r"[1-9][0-9]{0,19}")
MAX_COMMAND_STDOUT_BYTES = 32 * 1024 * 1024
MAX_COMMAND_STDERR_BYTES = 1024 * 1024
MAX_GITHUB_API_BYTES = 32 * 1024 * 1024
MAX_GITHUB_HELP_BYTES = 1024 * 1024
MAX_JOB_LOG_BYTES = 32 * 1024 * 1024
MAX_GITHUB_API_PAGES = 100
MAX_GITHUB_API_ITEMS = 10_000
MAX_RELEASE_METADATA_BYTES = 1024 * 1024
MAX_RELEASE_WORKFLOW_BYTES = 2 * 1024 * 1024
ALIAS_WORKFLOW_PATH = ".github/workflows/advance-release-alias.yml"
ACTIVE_PUBLICATION_STATES = {"requested", "pending", "queued", "in_progress", "waiting"}
GITHUB_ACTIONS_APP_ID = 15368
REQUIRED_PR_GATE_CONTEXT = "required-pr-gate"
ALIAS_CONTROL_PATHS = (
    "scripts/publish_release.py",
    "scripts/release_alias.py",
    "scripts/release_workflow.py",
    "scripts/_release_platform.py",
)
MAX_DISTRIBUTION_FILE_BYTES = 128 * 1024 * 1024
MAX_DISTRIBUTION_TOTAL_BYTES = 256 * 1024 * 1024
MAX_DISTRIBUTION_DIRECTORY_ENTRIES = 10_000
MAX_DISTRIBUTION_NAME_BYTES = 4 * 1024
MAX_DISTRIBUTION_TOTAL_NAME_BYTES = 1024 * 1024
MAX_JSON_INTEGER_DIGITS = 4300
MAX_TOML_INTEGER_DIGITS = 640
MAX_GITHUB_NUMERIC_ID = (1 << 64) - 1
_STREAM_CHUNK_BYTES = 64 * 1024
DISPATCH_DISCOVERY_ATTEMPTS = 12
DISPATCH_DISCOVERY_DELAY_SECONDS = 5.0
SURFACES = (
    "repository hygiene",
    "README and hosted documentation",
    "changelog and release notes",
    "schema URLs, configs, and locks",
    "CI and review state",
    "reproducible wheel, sdist, and standalone archive",
    "GitHub Action and Marketplace",
    "TestPyPI",
    "PyPI",
    "GitHub Release assets",
    "compatibility alias",
    "GHCR multi-platform container",
    "Homebrew tap",
    "GitLab CI/CD Catalog component",
    "pre-commit",
)


class GateError(RuntimeError):
    """A release prerequisite is absent, conflicting, or unreadable."""


def _bounded_json_int(value: str) -> int:
    """Parse a JSON integer independently of Python's mutable digit limit."""
    if re.fullmatch(r"-?(?:0|[1-9][0-9]*)", value) is None:
        raise ValueError("invalid JSON integer")
    negative = value.startswith("-")
    digits = value[1:] if negative else value
    if len(digits) > MAX_JSON_INTEGER_DIGITS:
        raise ValueError(
            "JSON integer exceeds the "
            f"{MAX_JSON_INTEGER_DIGITS}-decimal-digit limit"
        )
    result = 0
    for offset in range(0, len(digits), 9):
        chunk = digits[offset : offset + 9]
        result = result * (10 ** len(chunk)) + int(chunk)
    return -result if negative else result


def _bounded_json_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite JSON number is not supported")
    return result


def _reject_json_constant(_value: str) -> object:
    raise ValueError("non-finite JSON constant is not supported")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key is not supported")
        result[key] = value
    return result


def _strict_json_loads(document: str) -> object:
    return json.loads(
        document,
        object_pairs_hook=_unique_json_object,
        parse_constant=_reject_json_constant,
        parse_float=_bounded_json_float,
        parse_int=_bounded_json_int,
    )


def _toml_has_oversized_numeric_token(text: str) -> bool:
    """Find oversized TOML value tokens without examining strings or keys."""
    index = 0
    state = "normal"
    root_in_value = False
    table_header_depth = 0
    at_statement_start = True
    array_frame = 0
    inline_key_frame = 1
    inline_value_frame = 2
    containers = bytearray()

    def in_key_context() -> bool:
        if table_header_depth:
            return True
        if containers:
            return containers[-1] == inline_key_frame
        return not root_in_value

    def reset_line() -> None:
        nonlocal root_in_value, table_header_depth, at_statement_start
        if not containers:
            root_in_value = False
            table_header_depth = 0
            at_statement_start = True

    while index < len(text):
        char = text[index]
        if state == "comment":
            if char in "\r\n":
                state = "normal"
                reset_line()
            index += 1
            continue
        if state in {"basic", "multiline-basic"}:
            if char == "\\":
                index += 2
                continue
            if state == "basic" and char == '"':
                state = "normal"
                index += 1
                continue
            if state == "multiline-basic" and char == '"':
                end = index
                while end < len(text) and text[end] == '"':
                    end += 1
                if end - index >= 3:
                    state = "normal"
                index = end
                continue
            index += 1
            continue
        if state in {"literal", "multiline-literal"}:
            if state == "literal" and char == "'":
                state = "normal"
                index += 1
                continue
            if state == "multiline-literal" and char == "'":
                end = index
                while end < len(text) and text[end] == "'":
                    end += 1
                if end - index >= 3:
                    state = "normal"
                index = end
                continue
            index += 1
            continue

        if char == "#":
            state = "comment"
            index += 1
            continue
        if char in "\r\n":
            reset_line()
            index += 1
            continue
        if char in " \t":
            index += 1
            continue
        if text.startswith('\"\"\"', index):
            state = "multiline-basic"
            at_statement_start = False
            index += 3
            continue
        if text.startswith("'''", index):
            state = "multiline-literal"
            at_statement_start = False
            index += 3
            continue
        if char == '"':
            state = "basic"
            at_statement_start = False
            index += 1
            continue
        if char == "'":
            state = "literal"
            at_statement_start = False
            index += 1
            continue
        if char == "[":
            if table_header_depth:
                table_header_depth += 1
            elif not containers and not root_in_value and at_statement_start:
                table_header_depth = 1
            else:
                containers.append(array_frame)
            at_statement_start = False
            index += 1
            continue
        if char == "]":
            if table_header_depth:
                table_header_depth -= 1
            elif containers and containers[-1] == array_frame:
                containers.pop()
            at_statement_start = False
            index += 1
            continue
        if char == "{":
            containers.append(inline_key_frame)
            at_statement_start = False
            index += 1
            continue
        if char == "}":
            if containers and containers[-1] in {
                inline_key_frame,
                inline_value_frame,
            }:
                containers.pop()
            at_statement_start = False
            index += 1
            continue
        if char == "=":
            if containers and containers[-1] == inline_key_frame:
                containers[-1] = inline_value_frame
            elif not containers and not table_header_depth:
                root_in_value = True
            at_statement_start = False
            index += 1
            continue
        if char == ",":
            if containers and containers[-1] == inline_value_frame:
                containers[-1] = inline_key_frame
            at_statement_start = False
            index += 1
            continue
        if (
            not in_key_context()
            and char == "0"
            and index + 1 < len(text)
            and text[index + 1] in "bBoOxX"
        ):
            prefix = text[index + 1].lower()
            valid_digits = {
                "b": "01",
                "o": "01234567",
                "x": "0123456789abcdefABCDEF",
            }[prefix]
            index += 2
            digits = 0
            while index < len(text) and (
                text[index] in valid_digits or text[index] == "_"
            ):
                if text[index] != "_":
                    digits += 1
                    if digits > MAX_TOML_INTEGER_DIGITS:
                        return True
                index += 1
            continue
        if not in_key_context() and "0" <= char <= "9":
            digits = 0
            while index < len(text) and (
                "0" <= text[index] <= "9" or text[index] == "_"
            ):
                if text[index] != "_":
                    digits += 1
                    if digits > MAX_TOML_INTEGER_DIGITS:
                        return True
                index += 1
            continue
        at_statement_start = False
        index += 1
    return False


def _strict_toml_loads(document: str) -> dict[str, object]:
    if _toml_has_oversized_numeric_token(document):
        raise ValueError(
            "TOML numeric token exceeds the "
            f"{MAX_TOML_INTEGER_DIGITS}-digit cross-runtime safety limit"
        )
    return tomllib.loads(document)


def _is_positive_github_id(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 < value <= MAX_GITHUB_NUMERIC_ID
    )


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def _run_bytes(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    max_stdout_bytes: int | None = None,
    max_stderr_bytes: int | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run a command while enforcing independent hard stream ceilings."""
    stdout_limit = (
        MAX_COMMAND_STDOUT_BYTES
        if max_stdout_bytes is None
        else max_stdout_bytes
    )
    stderr_limit = (
        MAX_COMMAND_STDERR_BYTES
        if max_stderr_bytes is None
        else max_stderr_bytes
    )
    if stdout_limit < 0 or stderr_limit < 0:
        raise ValueError("subprocess output limits must be non-negative")

    argv = list(command)
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as error:
        raise GateError(f"required command is unavailable: {command[0]}") from error
    assert process.stdout is not None
    assert process.stderr is not None

    captured: dict[str, bytes] = {}
    overflows: list[str] = []
    read_errors: list[BaseException] = []
    state_lock = threading.Lock()

    def terminate() -> None:
        try:
            process.kill()
        except OSError:
            pass

    def read_stream(stream: BinaryIO, name: str, limit: int) -> None:
        data = bytearray()
        try:
            while True:
                remaining = limit - len(data)
                chunk = stream.read(min(_STREAM_CHUNK_BYTES, remaining + 1))
                if not chunk:
                    break
                if len(chunk) > remaining:
                    with state_lock:
                        overflows.append(name)
                    terminate()
                    break
                data.extend(chunk)
        except BaseException as error:
            with state_lock:
                read_errors.append(error)
            terminate()
        finally:
            captured[name] = bytes(data)
            stream.close()

    readers = (
        threading.Thread(
            target=read_stream,
            args=(process.stdout, "stdout", stdout_limit),
            daemon=True,
        ),
        threading.Thread(
            target=read_stream,
            args=(process.stderr, "stderr", stderr_limit),
            daemon=True,
        ),
    )
    for reader in readers:
        reader.start()
    try:
        returncode = process.wait()
    except BaseException:
        terminate()
        process.wait()
        raise
    finally:
        for reader in readers:
            reader.join()

    if read_errors:
        raise read_errors[0]
    if overflows:
        name = "stdout" if "stdout" in overflows else "stderr"
        limit = stdout_limit if name == "stdout" else stderr_limit
        raise GateError(
            f"{' '.join(command)}: {name} exceeds the {limit}-byte limit"
        )
    return subprocess.CompletedProcess(
        argv,
        returncode,
        captured["stdout"],
        captured["stderr"],
    )


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    check: bool = True,
    max_stdout_bytes: int | None = None,
    max_stderr_bytes: int | None = None,
) -> subprocess.CompletedProcess[str]:
    raw = _run_bytes(
        command,
        cwd=cwd,
        env=env,
        max_stdout_bytes=max_stdout_bytes,
        max_stderr_bytes=max_stderr_bytes,
    )
    encoding = locale.getpreferredencoding(False)

    def as_text(value: bytes) -> str:
        return value.decode(encoding).replace("\r\n", "\n").replace("\r", "\n")

    stdout = as_text(raw.stdout)
    stderr = as_text(raw.stderr)
    result = subprocess.CompletedProcess(raw.args, raw.returncode, stdout, stderr)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "command failed").strip()
        raise GateError(f"{' '.join(command)}: {detail}")
    return result


def _git(
    repo: Path,
    *arguments: str,
    check: bool = True,
    env: dict[str, str] | None = None,
    max_stdout_bytes: int | None = None,
) -> str:
    return _run(
        ("git", *arguments),
        cwd=repo,
        check=check,
        env=env,
        max_stdout_bytes=max_stdout_bytes,
    ).stdout.strip()


def _read_bounded_file(path: Path, label: str, *, max_bytes: int) -> bytes:
    """Read a stable regular file through a fixed-size buffer and sentinel."""
    if max_bytes < 0:
        raise ValueError("file byte limit must be non-negative")
    initial = path.lstat()
    if not stat.S_ISREG(initial.st_mode):
        raise GateError(f"{label} is not a regular file")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(opened.st_mode)
            or (initial.st_dev, initial.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise GateError(f"{label} changed while it was opened")
        if opened.st_size > max_bytes:
            raise GateError(f"{label} exceeds the {max_bytes}-byte limit")

        data = bytearray()
        while True:
            remaining = max_bytes - len(data)
            chunk = stream.read(min(_STREAM_CHUNK_BYTES, remaining + 1))
            if not chunk:
                break
            if len(chunk) > remaining:
                raise GateError(f"{label} exceeds the {max_bytes}-byte limit")
            data.extend(chunk)

        finished = os.fstat(stream.fileno())
    try:
        current = path.lstat()
    except FileNotFoundError as error:
        raise GateError(f"{label} disappeared while it was read") from error
    if (
        not stat.S_ISREG(current.st_mode)
        or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        or opened.st_size != finished.st_size
        or opened.st_mtime_ns != finished.st_mtime_ns
        or finished.st_size != len(data)
        or current.st_size != finished.st_size
        or current.st_mtime_ns != finished.st_mtime_ns
    ):
        raise GateError(f"{label} changed while it was read")
    return bytes(data)


def _read_bounded_text(path: Path, label: str, *, max_bytes: int) -> str:
    try:
        return _read_bounded_file(path, label, max_bytes=max_bytes).decode("utf-8")
    except UnicodeDecodeError as error:
        raise GateError(f"{label} is not valid UTF-8") from error


def _distribution_files(checkout: Path) -> tuple[Path, Path]:
    """Enumerate the candidate dist directory under entry/name ceilings."""
    directory = checkout / "dist"
    wheels: list[Path] = []
    sdists: list[Path] = []
    total_name_bytes = 0
    try:
        with os.scandir(directory) as entries:
            for entry_count, entry in enumerate(entries, start=1):
                if entry_count > MAX_DISTRIBUTION_DIRECTORY_ENTRIES:
                    raise GateError(
                        "candidate dist directory exceeds the "
                        f"{MAX_DISTRIBUTION_DIRECTORY_ENTRIES}-entry limit"
                    )
                name_bytes = os.fsencode(entry.name)
                if len(name_bytes) > MAX_DISTRIBUTION_NAME_BYTES:
                    raise GateError(
                        "candidate distribution name exceeds the "
                        f"{MAX_DISTRIBUTION_NAME_BYTES}-byte limit"
                    )
                total_name_bytes += len(name_bytes)
                if total_name_bytes > MAX_DISTRIBUTION_TOTAL_NAME_BYTES:
                    raise GateError(
                        "candidate dist names exceed the "
                        f"{MAX_DISTRIBUTION_TOTAL_NAME_BYTES}-byte aggregate limit"
                    )
                candidate = Path(entry.path)
                if entry.name.endswith(".whl"):
                    wheels.append(candidate)
                elif entry.name.endswith(".tar.gz"):
                    sdists.append(candidate)
    except FileNotFoundError as error:
        raise GateError("packaging smoke did not create a dist directory") from error
    if len(wheels) != 1 or len(sdists) != 1:
        raise GateError("packaging smoke did not create exactly one wheel and sdist")
    return sorted(wheels)[0], sorted(sdists)[0]


def _copy_bounded_distribution(
    source: Path,
    destination: Path,
    *,
    copied_bytes: int,
) -> int:
    """Copy one stable regular distribution within file and aggregate limits."""
    if copied_bytes < 0 or copied_bytes > MAX_DISTRIBUTION_TOTAL_BYTES:
        raise ValueError("copied distribution byte count is outside its limit")
    initial = source.lstat()
    if not stat.S_ISREG(initial.st_mode):
        raise GateError(f"candidate distribution is not a regular file: {source.name}")
    if initial.st_size > MAX_DISTRIBUTION_FILE_BYTES:
        raise GateError(
            f"candidate distribution {source.name} exceeds the "
            f"{MAX_DISTRIBUTION_FILE_BYTES}-byte file limit"
        )
    if copied_bytes + initial.st_size > MAX_DISTRIBUTION_TOTAL_BYTES:
        raise GateError(
            "candidate distributions exceed the "
            f"{MAX_DISTRIBUTION_TOTAL_BYTES}-byte aggregate limit"
        )

    copied = 0
    with source.open("rb") as source_stream:
        opened = os.fstat(source_stream.fileno())
        if (
            not stat.S_ISREG(opened.st_mode)
            or (initial.st_dev, initial.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise GateError(f"candidate distribution changed: {source.name}")
        with destination.open("xb") as destination_stream:
            while True:
                file_remaining = MAX_DISTRIBUTION_FILE_BYTES - copied
                aggregate_remaining = (
                    MAX_DISTRIBUTION_TOTAL_BYTES - copied_bytes - copied
                )
                remaining = min(file_remaining, aggregate_remaining)
                chunk = source_stream.read(
                    min(_STREAM_CHUNK_BYTES, remaining + 1)
                )
                if not chunk:
                    break
                if len(chunk) > remaining:
                    if len(chunk) > file_remaining:
                        raise GateError(
                            f"candidate distribution {source.name} exceeds the "
                            f"{MAX_DISTRIBUTION_FILE_BYTES}-byte file limit"
                        )
                    raise GateError(
                        "candidate distributions exceed the "
                        f"{MAX_DISTRIBUTION_TOTAL_BYTES}-byte aggregate limit"
                    )
                destination_stream.write(chunk)
                copied += len(chunk)
        finished = os.fstat(source_stream.fileno())
    try:
        current = source.lstat()
    except FileNotFoundError as error:
        raise GateError(
            f"candidate distribution disappeared while copying: {source.name}"
        ) from error
    if (
        not stat.S_ISREG(current.st_mode)
        or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        or opened.st_size != finished.st_size
        or opened.st_mtime_ns != finished.st_mtime_ns
        or finished.st_size != copied
        or current.st_size != finished.st_size
        or current.st_mtime_ns != finished.st_mtime_ns
    ):
        raise GateError(f"candidate distribution changed while copying: {source.name}")
    return copied_bytes + copied


def _head(repo: Path) -> str | None:
    result = _run(("git", "rev-parse", "--verify", "HEAD"), cwd=repo, check=False)
    value = result.stdout.strip()
    return value if result.returncode == 0 and SHA_RE.fullmatch(value) else None


def _sanitized_tool_environment(
    environment: dict[str, str] | None = None,
    *,
    sandbox_root: Path,
) -> dict[str, str]:
    """Build a minimal tool environment rooted in disposable directories."""
    source = os.environ if environment is None else environment
    by_upper_name = {name.upper(): value for name, value in source.items()}
    sanitized = {
        name: by_upper_name[name]
        for name in (
            "PATH",
            "PATHEXT",
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "SYSTEMDRIVE",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "TZ",
        )
        if name in by_upper_name
    }

    sandbox_root = sandbox_root.resolve()
    home = sandbox_root / "home"
    temporary = sandbox_root / "tmp"
    config = sandbox_root / "config"
    cache = sandbox_root / "cache"
    data = sandbox_root / "data"
    runtime = sandbox_root / "run"
    for directory in (home, temporary, config, cache, data, runtime):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    sanitized.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "APPDATA": str(config / "roaming"),
            "LOCALAPPDATA": str(config / "local"),
            "TMPDIR": str(temporary),
            "TEMP": str(temporary),
            "TMP": str(temporary),
            "XDG_CONFIG_HOME": str(config),
            "XDG_CACHE_HOME": str(cache),
            "XDG_DATA_HOME": str(data),
            "XDG_RUNTIME_DIR": str(runtime),
            "GH_CONFIG_DIR": str(config / "gh"),
            "DOCKER_CONFIG": str(config / "docker"),
            "KUBECONFIG": str(config / "kube" / "config"),
            "GNUPGHOME": str(config / "gnupg"),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
            "GH_HOST": "github.com",
            "GH_PAGER": "cat",
            "PAGER": "cat",
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_INDEX_URL": "https://pypi.org/simple",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
            "TERM": "dumb",
        }
    )
    if home.drive:
        sanitized["HOMEDRIVE"] = home.drive
        sanitized["HOMEPATH"] = str(home)[len(home.drive) :]
    return sanitized


def _canonical_origin(value: str) -> str | None:
    value = value.strip().removesuffix(".git")
    match = re.fullmatch(
        r"(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)"
        r"(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)",
        value,
    )
    return match.group("repo") if match else None


def _github_ref_pattern_matches(pattern: str, ref: str) -> bool:
    """Match GitHub ruleset ref patterns with slash-aware fnmatch semantics."""
    if pattern == "~ALL":
        return True
    pattern_parts = pattern.split("/")
    ref_parts = ref.split("/")

    def match(pattern_index: int, ref_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return ref_index == len(ref_parts)
        segment = pattern_parts[pattern_index]
        if segment == "**":
            return match(pattern_index + 1, ref_index) or (
                ref_index < len(ref_parts)
                and match(pattern_index, ref_index + 1)
            )
        return (
            ref_index < len(ref_parts)
            and fnmatch.fnmatchcase(ref_parts[ref_index], segment)
            and match(pattern_index + 1, ref_index + 1)
        )

    return match(0, 0)


def _ruleset_targets_ref(ref_name: object, ref: str) -> bool:
    if not isinstance(ref_name, dict):
        return False
    includes = ref_name.get("include")
    excludes = ref_name.get("exclude")
    if not isinstance(includes, list) or not all(
        isinstance(item, str) for item in includes
    ):
        return False
    if not isinstance(excludes, list) or not all(
        isinstance(item, str) for item in excludes
    ):
        return False
    return any(_github_ref_pattern_matches(item, ref) for item in includes) and not any(
        _github_ref_pattern_matches(item, ref) for item in excludes
    )


def _environment_requires_review(item: object) -> bool:
    rules = item.get("protection_rules") if isinstance(item, dict) else None
    return any(
        isinstance(rule, dict)
        and rule.get("type") == "required_reviewers"
        and isinstance(rule.get("reviewers"), list)
        and bool(rule["reviewers"])
        for rule in rules or []
    )


def _validate_tag_rulesets(rulesets: Sequence[dict], tag: str) -> None:
    exact_ref = f"refs/tags/{tag}"
    alias_ref = f"refs/tags/{tag.rsplit('.', 1)[0]}"
    exact_update = False
    exact_deletion = False
    exact_creation = False
    alias_mutation = False
    for detail in rulesets:
        rules = detail.get("rules")
        conditions = detail.get("conditions")
        rule_types = {
            rule.get("type") for rule in rules or [] if isinstance(rule, dict)
        }
        ref_name = conditions.get("ref_name") if isinstance(conditions, dict) else None
        if _ruleset_targets_ref(ref_name, exact_ref):
            exact_update = exact_update or "update" in rule_types
            exact_deletion = exact_deletion or "deletion" in rule_types
            exact_creation = exact_creation or "creation" in rule_types
        if _ruleset_targets_ref(ref_name, alias_ref):
            alias_mutation = alias_mutation or bool(
                {"update", "creation"} & rule_types
            )
    if not exact_update or not exact_deletion:
        raise GateError(
            "active tag rulesets must block update and deletion for the exact version tag"
        )
    if exact_creation:
        raise GateError(
            "an active creation restriction targets the exact version tag and can block the workflow"
        )
    if alias_mutation:
        raise GateError(
            "an active creation/update restriction targets the mutable vMAJOR.MINOR alias"
        )


def _validate_main_branch_rulesets(rulesets: Sequence[dict]) -> None:
    """Require one no-bypass ruleset carrying the complete main contract."""
    main_ref = "refs/heads/main"
    for detail in rulesets:
        conditions = detail.get("conditions")
        ref_name = conditions.get("ref_name") if isinstance(conditions, dict) else None
        if not _ruleset_targets_ref(ref_name, main_ref):
            continue
        if detail.get("bypass_actors") != []:
            continue
        rules = detail.get("rules")
        if not isinstance(rules, list):
            continue
        by_type = {
            rule.get("type"): rule for rule in rules if isinstance(rule, dict)
        }
        pull_parameters = by_type.get("pull_request", {}).get("parameters")
        status_parameters = by_type.get("required_status_checks", {}).get(
            "parameters"
        )
        if not isinstance(pull_parameters, dict) or not isinstance(
            status_parameters, dict
        ):
            continue
        checks = status_parameters.get("required_status_checks")
        required_gate = any(
            isinstance(check, dict)
            and check.get("context") == REQUIRED_PR_GATE_CONTEXT
            and check.get("integration_id") == GITHUB_ACTIONS_APP_ID
            for check in checks or []
        )
        if (
            "deletion" in by_type
            and "non_fast_forward" in by_type
            and pull_parameters.get("required_review_thread_resolution") is True
            and "squash" in pull_parameters.get("allowed_merge_methods", [])
            and status_parameters.get("strict_required_status_checks_policy") is True
            and required_gate
        ):
            return
    raise GateError(
        "an active no-bypass main ruleset must require pull requests with "
        "resolved conversations, squash merges, the strict GitHub Actions "
        "required-pr-gate, and block deletion and force pushes"
    )


def _remote_ref(repo: Path, remote: str, ref: str) -> str | None:
    fields = _git(repo, "ls-remote", remote, ref).split()
    if not fields:
        return None
    if len(fields) != 2 or fields[1] != ref or SHA_RE.fullmatch(fields[0]) is None:
        raise GateError(f"remote returned malformed ref data for {ref}")
    return fields[0]


def _gh_json(repo: Path, repository: str, endpoint: str) -> object:
    result = _run(
        ("gh", "api", endpoint),
        cwd=repo,
        check=False,
        max_stdout_bytes=MAX_GITHUB_API_BYTES,
    )
    if result.returncode != 0:
        raise GateError(
            f"GitHub API failed for {endpoint}: "
            f"{(result.stderr or result.stdout).strip() or 'unknown error'}"
        )
    try:
        return _strict_json_loads(result.stdout)
    except (json.JSONDecodeError, ValueError) as error:
        raise GateError(f"GitHub API returned invalid JSON for {endpoint}") from error


def _gh_paginated_list(
    repo: Path,
    endpoint: str,
    *,
    collection: str | None = None,
) -> list[object]:
    """Read every REST page as one bounded list, failing closed on shape drift."""
    result = _run(
        ("gh", "api", "--paginate", "--slurp", endpoint),
        cwd=repo,
        check=False,
        max_stdout_bytes=MAX_GITHUB_API_BYTES,
    )
    if result.returncode != 0:
        raise GateError(
            f"GitHub API failed for {endpoint}: "
            f"{(result.stderr or result.stdout).strip() or 'unknown error'}"
        )
    try:
        pages = _strict_json_loads(result.stdout)
    except (json.JSONDecodeError, ValueError) as error:
        raise GateError(f"GitHub API returned invalid JSON for {endpoint}") from error
    if not isinstance(pages, list) or len(pages) > MAX_GITHUB_API_PAGES:
        raise GateError(f"GitHub API pagination is malformed or excessive for {endpoint}")
    items: list[object] = []
    for page in pages:
        values = page.get(collection) if collection and isinstance(page, dict) else page
        if not isinstance(values, list):
            raise GateError(f"GitHub API page is not a list for {endpoint}")
        items.extend(values)
        if len(items) > MAX_GITHUB_API_ITEMS:
            raise GateError(f"GitHub API returned too many items for {endpoint}")
    return items


def _github_release_for_tag(repo: Path, tag: str) -> dict | None:
    """Find one authenticated draft/public release and bind reads to its ID."""
    endpoint = f"repos/{REPOSITORY}/releases?per_page=100"
    releases = _gh_paginated_list(repo, endpoint)
    matches = [
        item
        for item in releases
        if isinstance(item, dict) and item.get("tag_name") == tag
    ]
    if len(matches) > 1:
        raise GateError(f"GitHub returned multiple Releases for exact tag {tag}")
    if not matches:
        return None
    release_id = matches[0].get("id")
    if not _is_positive_github_id(release_id):
        raise GateError("GitHub Release has an invalid numeric ID")
    detail = _gh_json(repo, REPOSITORY, f"repos/{REPOSITORY}/releases/{release_id}")
    if (
        not isinstance(detail, dict)
        or detail.get("id") != release_id
        or detail.get("tag_name") != tag
    ):
        raise GateError("GitHub Release detail disagrees with the paginated release list")
    return detail


def _dispatch_title(
    workflow: str,
    tag: str,
    sha: str,
    alias: str,
    resume_run_id: int | None,
) -> str:
    if workflow == "create-release-tag.yml" and resume_run_id is None:
        return f"release-tag:{tag}@{sha}:alias={alias}"
    if workflow == "publish.yml" and resume_run_id is not None:
        return f"publish:{tag}@{sha}:alias={alias}:resume={resume_run_id}"
    raise GateError("workflow dispatch identity is internally inconsistent")


def _find_dispatch_run(
    repo: Path,
    workflow: str,
    control_sha: str,
    title: str,
) -> dict | None:
    endpoint = (
        f"repos/{REPOSITORY}/actions/workflows/{workflow}/runs"
        f"?event=workflow_dispatch&head_sha={control_sha}&per_page=100"
    )
    runs = _gh_paginated_list(repo, endpoint, collection="workflow_runs")
    matches = [
        run
        for run in runs
        if isinstance(run, dict)
        and run.get("event") == "workflow_dispatch"
        and run.get("head_branch") == "main"
        and run.get("head_sha") == control_sha
        and run.get("display_title") == title
    ]
    if len(matches) > 1:
        raise GateError("multiple workflow runs match the exact dispatch identity")
    if not matches:
        return None
    run = matches[0]
    run_id = run.get("id")
    url = run.get("html_url")
    if (
        not _is_positive_github_id(run_id)
        or not isinstance(url, str)
        or not url.startswith(f"https://github.com/{REPOSITORY}/actions/runs/")
    ):
        raise GateError("matching workflow run has malformed identity metadata")
    return run


def _dispatch_workflow(
    repo: Path,
    command: Sequence[str],
    *,
    workflow: str,
    control_sha: str,
    title: str,
) -> str:
    existing = _find_dispatch_run(repo, workflow, control_sha, title)
    if existing is not None:
        return f"exact workflow dispatch already exists at {existing['html_url']}"

    result = _run(command, cwd=repo, check=False)
    for attempt in range(DISPATCH_DISCOVERY_ATTEMPTS):
        accepted = _find_dispatch_run(repo, workflow, control_sha, title)
        if accepted is not None:
            return f"workflow dispatch accepted at {accepted['html_url']}"
        if attempt + 1 < DISPATCH_DISCOVERY_ATTEMPTS:
            time.sleep(DISPATCH_DISCOVERY_DELAY_SECONDS)

    detail = (result.stderr or result.stdout).strip()
    if result.returncode != 0:
        raise GateError(
            "workflow dispatch response was ambiguous and no exact run appeared: "
            + (detail or "unknown error")
        )
    raise GateError("workflow dispatch returned success but no exact run appeared")


def _gh_job_log(repo: Path, job_id: int) -> str:
    if not _is_positive_github_id(job_id):
        raise GateError("source verify-release job ID is malformed")
    endpoint = f"repos/{REPOSITORY}/actions/jobs/{job_id}/logs"
    help_result = _run_bytes(
        ("gh", "api", "--help"),
        cwd=repo,
        max_stdout_bytes=MAX_GITHUB_HELP_BYTES,
        max_stderr_bytes=MAX_GITHUB_HELP_BYTES,
    )
    if help_result.returncode != 0:
        detail = (help_result.stderr or help_result.stdout).decode(
            "utf-8", errors="replace"
        )
        raise GateError(
            "cannot inspect GitHub CLI API capabilities: "
            f"{detail.strip() or 'unknown error'}"
        )
    help_output = (help_result.stdout or b"") + (help_result.stderr or b"")
    command = ["gh", "api"]
    if b"--allow-escape-sequences" in help_output:
        command.append("--allow-escape-sequences")
    command.append(endpoint)
    result = _run_bytes(
        command,
        cwd=repo,
        max_stdout_bytes=MAX_JOB_LOG_BYTES,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).decode("utf-8", errors="replace")
        raise GateError(
            f"GitHub API failed for {endpoint}: "
            f"{detail.strip() or 'unknown error'}"
        )
    if not result.stdout:
        raise GateError("source verify-release job log is empty")
    if len(result.stdout) > MAX_JOB_LOG_BYTES:
        raise GateError("source verify-release job log exceeds the inspection limit")
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError as error:
        raise GateError("source verify-release job log is not valid UTF-8") from error


def _record(checks: list[Check], name: str, operation) -> object | None:
    try:
        detail = operation()
    except (GateError, OSError, ValueError, KeyError, TypeError) as error:
        checks.append(Check(name, "failed", str(error)))
        return None
    checks.append(Check(name, "passed", str(detail or "passed")))
    return detail


def _project(repo: Path, tag: str) -> str:
    try:
        metadata = _strict_toml_loads(
            _read_bounded_text(
                repo / "pyproject.toml",
                "pyproject.toml",
                max_bytes=MAX_RELEASE_METADATA_BYTES,
            )
        )
        project = metadata["project"]
        if not isinstance(project, dict):
            raise TypeError("project metadata must be a TOML table")
    except (
        OSError,
        UnicodeError,
        KeyError,
        TypeError,
        ValueError,
        tomllib.TOMLDecodeError,
    ) as error:
        raise GateError(f"cannot read pyproject.toml metadata: {error}") from error
    if project.get("name") != "boundver" or project.get("version") != tag[1:]:
        raise GateError("pyproject name/version does not match boundver and the release tag")
    return f"boundver {project['version']}"


def _project_at_commit(repo: Path, sha: str, tag: str) -> str:
    try:
        metadata = _git(
            repo,
            "show",
            f"{sha}:pyproject.toml",
            max_stdout_bytes=MAX_RELEASE_METADATA_BYTES,
        )
        parsed = _strict_toml_loads(metadata)
        project = parsed["project"]
        if not isinstance(project, dict):
            raise TypeError("project metadata must be a TOML table")
    except (
        OSError,
        UnicodeError,
        KeyError,
        TypeError,
        ValueError,
        tomllib.TOMLDecodeError,
    ) as error:
        raise GateError(
            f"cannot read pyproject.toml metadata at release commit {sha}: {error}"
        ) from error
    if project.get("name") != "boundver" or project.get("version") != tag[1:]:
        raise GateError(
            "release-commit pyproject name/version does not match boundver and the release tag"
        )
    return f"boundver {project['version']} at {sha}"


def _release_alias_workflow_at_commit(
    repo: Path, remote: str, release_sha: str, tag: str, alias: str
) -> str:
    """Require exact-tag alias handoff verification in the immutable release."""
    if alias == "none":
        return "no compatibility alias was requested"
    entry = _git(
        repo,
        "ls-tree",
        "--full-tree",
        release_sha,
        "--",
        ALIAS_WORKFLOW_PATH,
        max_stdout_bytes=1024,
    )
    if not entry:
        if _remote_ref(repo, remote, f"refs/tags/{alias}") == release_sha:
            return f"{alias} already resolves to immutable release {release_sha}"
        raise GateError(
            f"immutable release {tag} at {release_sha} predates the exact-tag "
            "alias verification workflow; recovery cannot independently verify "
            f"the {alias} maintainer handoff"
        )
    header, separator, path = entry.partition("\t")
    fields = header.split()
    if (
        separator != "\t"
        or len(fields) != 3
        or fields[0] not in {"100644", "100755"}
        or fields[1] != "blob"
        or SHA_RE.fullmatch(fields[2]) is None
        or path != ALIAS_WORKFLOW_PATH
    ):
        raise GateError(
            f"immutable release {tag} does not contain a regular "
            f"{ALIAS_WORKFLOW_PATH} workflow"
        )
    return f"{ALIAS_WORKFLOW_PATH} is present at immutable release {release_sha}"


def _clean(repo: Path) -> str:
    state = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    if state:
        raise GateError("worktree and index must be clean (tracked, staged, and untracked)")
    git_dir = Path(_git(repo, "rev-parse", "--git-dir"))
    if not git_dir.is_absolute():
        git_dir = repo / git_dir
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"):
        if (git_dir / marker).exists():
            raise GateError(f"Git operation is still active: {marker}")
    return "clean worktree/index; no merge, rebase, cherry-pick, or revert"


def _repository_hygiene(repo: Path) -> str:
    with tempfile.TemporaryDirectory(prefix="boundver-hygiene-env-") as temporary:
        tool_env = _sanitized_tool_environment(sandbox_root=Path(temporary))
        result = _run(
            (sys.executable, "-I", "scripts/check_repo_hygiene.py", "--repo", "."),
            cwd=repo,
            env=tool_env,
        )
    return result.stdout.strip() or "tracked repository tree is portable and clean"


def _repo_identity(repo: Path, remote: str) -> str:
    if Path(_git(repo, "rev-parse", "--show-toplevel")).resolve() != repo.resolve():
        raise GateError("--repo must be the repository root")
    origin = _git(repo, "remote", "get-url", remote)
    if _canonical_origin(origin) != REPOSITORY:
        raise GateError(f"{remote} is not canonical repository {REPOSITORY}")
    return f"{REPOSITORY} via {remote}"


def _main_identity(repo: Path, remote: str, sha: str) -> str:
    if _git(repo, "symbolic-ref", "--short", "HEAD") != "main":
        raise GateError("release checks must run from branch main")
    if _head(repo) != sha:
        raise GateError("HEAD changed during release checks")
    main = _remote_ref(repo, remote, "refs/heads/main")
    if main != sha:
        raise GateError(f"HEAD {sha} is not current remote main {main}")
    return sha


def _remote_release_state(repo: Path, remote: str, tag: str) -> str:
    tag_sha = _remote_ref(repo, remote, f"refs/tags/{tag}")
    if tag_sha is not None:
        raise GateError(f"exact tag already exists at {tag_sha}; use the original run to resume")
    branch = _remote_ref(repo, remote, f"refs/heads/release/{tag}")
    if branch is not None:
        raise GateError(f"legacy release branch already exists at {branch}; inspect its run")
    return "exact tag and legacy release branch are absent"


def _github_controls(
    repo: Path,
    sha: str,
    tag: str,
    *,
    allow_resumable_release: bool = False,
    expected_active_publication_run_id: int | None = None,
) -> str:
    if expected_active_publication_run_id is not None and not _is_positive_github_id(
        expected_active_publication_run_id
    ):
        raise GateError("expected active publication run ID is malformed")
    metadata = _gh_json(repo, REPOSITORY, f"repos/{REPOSITORY}")
    if not isinstance(metadata, dict) or metadata.get("full_name") != REPOSITORY:
        raise GateError("authenticated GitHub repository identity disagrees")
    if metadata.get("default_branch") != "main" or metadata.get("archived") is not False:
        raise GateError("GitHub repository must be active with main as default branch")
    if metadata.get("visibility") != "public":
        raise GateError("GitHub repository must be public before release promotion")
    if metadata.get("homepage") != "https://github.com/marketplace/actions/boundver":
        raise GateError("GitHub repository homepage must point to the Marketplace listing")
    if not isinstance(metadata.get("description"), str) or not metadata["description"].strip():
        raise GateError("GitHub repository description must be populated")
    topics = metadata.get("topics")
    required_topics = {"api-compatibility", "ci", "openapi", "semantic-versioning"}
    if not isinstance(topics, list) or not required_topics <= set(topics):
        raise GateError(
            "GitHub repository topics must include " + ", ".join(sorted(required_topics))
        )
    values = _gh_paginated_list(
        repo,
        f"repos/{REPOSITORY}/environments?per_page=100",
        collection="environments",
    )
    by_name = {item.get("name"): item for item in values if isinstance(item, dict)}
    for name in (
        "testpypi",
        "pypi",
        "marketplace",
        "container",
        "container-public",
        "action-alias",
    ):
        item = by_name.get(name)
        if not _environment_requires_review(item):
            raise GateError(
                f"GitHub environment {name!r} must require at least one reviewer"
            )
    pages = _gh_json(repo, REPOSITORY, f"repos/{REPOSITORY}/pages")
    if (
        not isinstance(pages, dict)
        or pages.get("build_type") != "workflow"
        or pages.get("html_url") != "https://yzm1.github.io/boundver/"
        or pages.get("https_enforced") is not True
        or pages.get("public") is not True
    ):
        raise GateError(
            "GitHub Pages must use the public HTTPS GitHub Actions deployment"
        )
    tap = _gh_json(repo, HOMEBREW_REPOSITORY, f"repos/{HOMEBREW_REPOSITORY}")
    if (
        not isinstance(tap, dict)
        or tap.get("full_name") != HOMEBREW_REPOSITORY
        or tap.get("default_branch") != "main"
        or tap.get("archived") is not False
        or tap.get("visibility") != "public"
    ):
        raise GateError("the canonical Homebrew tap must be public and active")
    for path in ("Formula/boundver.rb", ".github/workflows/update-formula.yml"):
        item = _gh_json(
            repo,
            HOMEBREW_REPOSITORY,
            f"repos/{HOMEBREW_REPOSITORY}/contents/{path}?ref=main",
        )
        if (
            not isinstance(item, dict)
            or item.get("type") != "file"
            or not isinstance(item.get("sha"), str)
            or SHA_RE.fullmatch(item["sha"]) is None
            or not isinstance(item.get("size"), int)
            or not 0 < item["size"] <= MAX_RELEASE_WORKFLOW_BYTES
        ):
            raise GateError(f"Homebrew tap contract is absent or malformed: {path}")
    tap_environment = _gh_json(
        repo,
        HOMEBREW_REPOSITORY,
        f"repos/{HOMEBREW_REPOSITORY}/environments/formula-update",
    )
    if not _environment_requires_review(tap_environment):
        raise GateError(
            "Homebrew formula-update environment must require at least one reviewer"
        )
    immutable = _gh_json(repo, REPOSITORY, f"repos/{REPOSITORY}/immutable-releases")
    if not isinstance(immutable, dict) or immutable.get("enabled") is not True:
        raise GateError("immutable GitHub Releases are not enabled")
    rulesets = _gh_paginated_list(
        repo,
        f"repos/{REPOSITORY}/rulesets?includes_parents=true&per_page=100",
    )
    tag_rulesets: list[dict] = []
    branch_rulesets: list[dict] = []
    for summary in rulesets:
        if not isinstance(summary, dict) or summary.get("target") not in {
            "tag",
            "branch",
        }:
            continue
        if summary.get("enforcement") != "active":
            continue
        ruleset_id = summary.get("id")
        if not _is_positive_github_id(ruleset_id):
            continue
        detail = _gh_json(repo, REPOSITORY, f"repos/{REPOSITORY}/rulesets/{ruleset_id}")
        if not isinstance(detail, dict):
            continue
        if summary.get("target") == "tag":
            tag_rulesets.append(detail)
        else:
            branch_rulesets.append(detail)
    _validate_tag_rulesets(tag_rulesets, tag)
    _validate_main_branch_rulesets(branch_rulesets)
    workflow_runs = _gh_paginated_list(
        repo,
        f"repos/{REPOSITORY}/actions/workflows/ci.yml/runs"
        f"?head_sha={sha}&event=push&per_page=100",
        collection="workflow_runs",
    )
    if not any(
        isinstance(run, dict)
        and run.get("head_sha") == sha
        and run.get("head_branch") == "main"
        and run.get("event") == "push"
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
        for run in workflow_runs
    ):
        raise GateError("no successful completed ci.yml push run for exact main SHA")
    active_states = {"requested", "pending", "queued", "in_progress", "waiting"}
    for workflow in (
        "create-release-tag.yml",
        "publish.yml",
        "publish-container.yml",
    ):
        runs_value = _gh_paginated_list(
            repo,
            f"repos/{REPOSITORY}/actions/workflows/{workflow}/runs?per_page=100",
            collection="workflow_runs",
        )
        active_runs = [
            run
            for run in runs_value
            if isinstance(run, dict) and run.get("status") in active_states
        ]
        if workflow == "publish.yml" and expected_active_publication_run_id is not None:
            if (
                len(active_runs) != 1
                or active_runs[0].get("id") != expected_active_publication_run_id
            ):
                raise GateError(
                    "the expected publication must be the only active publish.yml run"
                )
            continue
        if active_runs:
            raise GateError(f"another release operation is active in {workflow}")
    release_detail = _github_release_for_tag(repo, tag)
    if release_detail is not None:
        if not allow_resumable_release:
            raise GateError("a GitHub Release already exists; use the original run to resume")
        if (
            not isinstance(release_detail.get("draft"), bool)
        ):
            raise GateError("GitHub Release state is malformed or disagrees with the tag")
        if release_detail["draft"] is False and (
            release_detail.get("immutable") is not True
            or release_detail.get("prerelease") is not False
            or not isinstance(release_detail.get("published_at"), str)
            or not release_detail["published_at"]
        ):
            raise GateError(
                "an existing public GitHub Release must be stable and immutable "
                "before recovery"
            )
    return (
        "repository, exact CI, Pages, Homebrew tap, environments, immutability, "
        "and promotion state verified"
    )


def _resume_release_state(repo: Path, remote: str, tag: str, sha: str) -> str:
    tag_sha = _remote_ref(repo, remote, f"refs/tags/{tag}")
    if tag_sha is None:
        raise GateError(f"exact tag {tag} is absent; only the original start path may create it")
    if tag_sha != sha:
        raise GateError(f"exact tag {tag} resolves to {tag_sha}, not release SHA {sha}")
    branch = _remote_ref(repo, remote, f"refs/heads/release/{tag}")
    if branch is not None:
        raise GateError(f"legacy release branch already exists at {branch}; inspect its run")
    return f"exact tag resolves to {sha}; legacy release branch is absent"


def _release_is_on_main(repo: Path, release_sha: str, main_sha: str) -> str:
    result = _run(
        ("git", "merge-base", "--is-ancestor", release_sha, main_sha),
        cwd=repo,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if result.returncode == 1:
            detail = "release commit is not an ancestor of current main"
        raise GateError(detail or "cannot prove that the release commit is on main")
    return f"release commit {release_sha} is an ancestor of current main {main_sha}"


def _active_alias_publication(
    repo: Path,
    run_id: int,
    tag: str,
    release_sha: str,
    control_sha: str,
) -> dict[str, object]:
    """Read the exact active publication identity used by the alias handoff."""
    payload = _gh_json(repo, REPOSITORY, f"repos/{REPOSITORY}/actions/runs/{run_id}")
    if not isinstance(payload, dict):
        raise GateError("alias publication run is malformed")
    attempt = payload.get("run_attempt")
    if not _is_positive_github_id(payload.get("id")) or payload["id"] != run_id:
        raise GateError("alias publication run ID is malformed or different")
    if not _is_positive_github_id(attempt):
        raise GateError("alias publication attempt is malformed")
    repository = payload.get("repository")
    publication_ref = payload.get("head_branch")
    publication_sha = payload.get("head_sha")
    if (
        payload.get("event") != "workflow_dispatch"
        or payload.get("path") != ".github/workflows/publish.yml"
        or payload.get("status") not in ACTIVE_PUBLICATION_STATES
        or payload.get("conclusion") is not None
        or not isinstance(repository, dict)
        or repository.get("full_name") != REPOSITORY
        or publication_ref not in {tag, "main"}
        or not isinstance(publication_sha, str)
        or SHA_RE.fullmatch(publication_sha) is None
    ):
        raise GateError("alias publication is not the active exact publish workflow")
    _require_reviewed_alias_control(repo, publication_sha, control_sha)
    if publication_ref == tag:
        if publication_sha != release_sha:
            raise GateError("exact-tag publication SHA differs from the release SHA")
    return {
        "run_id": run_id,
        "attempt": attempt,
        "ref": publication_ref,
        "sha": publication_sha,
        "detail": (
            f"active publish run {run_id} attempt {attempt} is controlled by "
            f"{publication_ref} at {publication_sha}"
        ),
    }


def _require_reviewed_alias_control(
    repo: Path, publication_sha: str, control_sha: str
) -> str:
    """Allow later main commits only when the credentialed control code is identical."""
    ancestry = _run(
        ("git", "merge-base", "--is-ancestor", publication_sha, control_sha),
        cwd=repo,
        check=False,
    )
    if ancestry.returncode != 0:
        raise GateError("active publication control is not an ancestor of current main")
    for path in ALIAS_CONTROL_PATHS:
        publication_blob = _git(
            repo,
            "rev-parse",
            f"{publication_sha}:{path}",
            max_stdout_bytes=1024,
        )
        current_blob = _git(
            repo,
            "rev-parse",
            f"{control_sha}:{path}",
            max_stdout_bytes=1024,
        )
        if (
            SHA_RE.fullmatch(publication_blob) is None
            or SHA_RE.fullmatch(current_blob) is None
        ):
            raise GateError(f"cannot bind reviewed alias control file {path}")
        if publication_blob != current_blob:
            raise GateError(
                f"alias control file {path} changed after publication dispatch; "
                "resume publication from reviewed current main"
            )
    return "credentialed alias-control scripts match the reviewed publication commit"


def _authenticated_alias_actor(repo: Path) -> str:
    payload = _gh_json(repo, REPOSITORY, "user")
    owner = REPOSITORY.partition("/")[0]
    if (
        not isinstance(payload, dict)
        or payload.get("login") != owner
        or payload.get("type") != "User"
    ):
        raise GateError(
            f"compatibility alias mutation must authenticate gh as repository owner {owner}"
        )
    return f"gh is authenticated as repository owner {owner}"


def _advance_alias_locally(
    repo: Path,
    tag: str,
    release_sha: str,
    alias: str,
    publication: dict[str, object],
) -> str:
    """Apply the reviewed local handoff without exposing credentials to candidate code."""
    _authenticated_alias_actor(repo)
    _run(("gh", "auth", "setup-git", "--hostname", "github.com"), cwd=repo)
    authenticated_remote = f"https://github.com/{REPOSITORY}.git"
    environment = os.environ.copy()
    environment.pop(REVIEW_TOKEN_ENV, None)
    result = _run(
        (
            sys.executable,
            "-I",
            str(repo / "scripts" / "release_alias.py"),
            "advance",
            "--repo-root",
            str(repo),
            "--repository",
            REPOSITORY,
            "--remote",
            authenticated_remote,
            "--tag",
            tag,
            "--sha",
            release_sha,
            "--alias",
            alias,
            "--publication-run-id",
            str(publication["run_id"]),
            "--publication-attempt",
            str(publication["attempt"]),
            "--publication-ref",
            str(publication["ref"]),
            "--publication-sha",
            str(publication["sha"]),
        ),
        cwd=repo,
        env=environment,
        max_stdout_bytes=1024 * 1024,
    )
    return result.stdout.strip() or f"advanced {alias} to {release_sha}"


def _require_source_release_inputs(
    job_log: str,
    tag: str,
    sha: str,
    alias: str,
) -> str:
    """Compatibility adapter for the extracted release-log validator."""
    try:
        return release_workflow.require_release_input_evidence(
            job_log,
            tag=tag,
            sha=sha,
            alias=alias,
        )
    except ReleaseWorkflowError as error:
        raise GateError(str(error)) from error


def _source_release_artifacts(
    repo: Path,
    run_id: int,
    tag: str,
    sha: str,
    alias: str,
) -> str:
    """Bind a failed publication to its exact retained verified artifacts."""
    run_endpoint = f"repos/{REPOSITORY}/actions/runs/{run_id}"
    run = _gh_json(repo, REPOSITORY, run_endpoint)
    jobs_payload = _gh_json(
        repo,
        REPOSITORY,
        f"{run_endpoint}/jobs?filter=all&per_page=100",
    )
    artifacts_payload = _gh_json(
        repo,
        REPOSITORY,
        f"{run_endpoint}/artifacts?per_page=100",
    )
    try:
        selection = release_workflow.select_recovery_artifacts(
            run,
            jobs_payload,
            artifacts_payload,
            repository=REPOSITORY,
            run_id=run_id,
            tag=tag,
            sha=sha,
        )
    except ReleaseWorkflowError as error:
        raise GateError(str(error)) from error
    source_inputs = _require_source_release_inputs(
        _gh_job_log(repo, selection.verification_job_id),
        tag,
        sha,
        alias,
    )
    return (
        f"failed publish run {run_id} attempt {selection.source_run_attempt} "
        f"reuses successful verify-release attempt {selection.artifact_attempt} "
        "and its two exact unexpired artifacts; "
        f"validated {selection.release_note_artifact_count} retained "
        f"release-note artifact(s) and {selection.downstream_artifact_count} "
        f"downstream artifact(s); {source_inputs}"
    )


def _disposable_gate(repo: Path, remote: str, sha: str, tag: str) -> str:
    token = os.environ.get(REVIEW_TOKEN_ENV, "")
    if not token or token != token.strip() or any(character.isspace() for character in token):
        raise GateError(
            f"{REVIEW_TOKEN_ENV} must contain an explicit fine-grained, read-only "
            "token for the release review audit"
        )
    # Keep the Windows path budget small.  The packaging smoke creates nested
    # virtual environments, and build-tool wheels can contain paths more than
    # 130 characters below those environments.
    with tempfile.TemporaryDirectory(prefix="bv-rel-") as temporary:
        checkout = Path(temporary) / "c"
        tool_env = _sanitized_tool_environment(
            sandbox_root=Path(temporary) / "e"
        )
        bash = resolve_bash(tool_env.get("PATH"))
        if bash is None:
            raise GateError(
                "Git-for-Windows Bash is required on Windows; a WSL launcher is "
                "not compatible with the local release gate"
            )
        configured_source = _git(repo, "remote", "get-url", remote, env=tool_env)
        if _canonical_origin(configured_source) != REPOSITORY:
            raise GateError(f"{remote} is not canonical repository {REPOSITORY}")
        source = f"https://github.com/{REPOSITORY}.git"
        _run(
            ("git", "clone", "--quiet", source, str(checkout)),
            cwd=repo,
            env=tool_env,
        )
        _run(
            ("git", "checkout", "--quiet", "--detach", sha),
            cwd=checkout,
            env=tool_env,
        )
        checked_out_sha = _run(
            ("git", "rev-parse", "--verify", "HEAD"),
            cwd=checkout,
            env=tool_env,
        ).stdout.strip()
        if checked_out_sha != sha:
            raise GateError("disposable checkout did not resolve the release SHA")
        # Keep the credentialed audit on the already-reviewed control checkout.
        # The candidate checkout receives only the credential-free tool environment.
        audit_env = tool_env.copy()
        audit_env["GITHUB_REPOSITORY"] = REPOSITORY
        audit_env["GH_TOKEN"] = token
        audit_env["PYTHON"] = Path(sys.executable).resolve().as_posix()
        _run(
            (bash, "scripts/audit_release_reviews.sh", sha, tag),
            cwd=repo,
            env=audit_env,
        )
        if tag == "v0.15.0":
            _run(
                (
                    sys.executable,
                    "-I",
                    "scripts/audit_semantic_provider_proposal.py",
                    "--gate",
                    "v0.15-release",
                    "--release-tag",
                    tag,
                    "--release-sha",
                    sha,
                ),
                cwd=repo,
                env=audit_env,
            )
        tooling = Path(temporary) / "t"
        _run(
            (sys.executable, "-I", "-m", "venv", str(tooling)),
            cwd=checkout,
            env=tool_env,
        )
        if os.name == "nt":
            tooling_python = tooling / "Scripts" / "python.exe"
            tooling_bin = tooling / "Scripts"
        else:
            tooling_python = tooling / "bin" / "python"
            tooling_bin = tooling / "bin"
        _run(
            (
                str(tooling_python),
                "-I",
                "scripts/install_locked_tools.py",
                "release",
            ),
            cwd=checkout,
            env=tool_env,
        )
        _run(
            (
                str(tooling_python),
                "-I",
                "-m",
                "pip",
                "--isolated",
                "install",
                "--disable-pip-version-check",
                "--no-index",
                "--no-deps",
                "--no-build-isolation",
                "-e",
                ".",
            ),
            cwd=checkout,
            env=tool_env,
        )
        tool_env["PATH"] = str(tooling_bin) + os.pathsep + tool_env.get("PATH", "")
        _run(
            (
                str(tooling_python),
                "-I",
                "scripts/verify_release_candidate.py",
                "--tag",
                tag,
                "--release-sha",
                sha,
            ),
            cwd=checkout,
            env=tool_env,
        )
        distributions = _distribution_files(checkout)
        python_dist = checkout / "python-dist"
        python_dist.mkdir()
        copied_bytes = 0
        for distribution in distributions:
            copied_bytes = _copy_bounded_distribution(
                distribution,
                python_dist / distribution.name,
                copied_bytes=copied_bytes,
            )
        for api_base, origin in (
            ("https://test.pypi.org/pypi", "https://test-files.pythonhosted.org"),
            ("https://pypi.org/pypi", "https://files.pythonhosted.org"),
        ):
            preflight = _run(
                (
                    str(tooling_python),
                    "-I",
                    "scripts/verify_testpypi_release.py",
                    "preflight",
                    "--dist",
                    "python-dist",
                    "--project",
                    "boundver",
                    "--version",
                    tag[1:],
                    "--api-base",
                    api_base,
                    "--download-origin",
                    origin,
                ),
                cwd=checkout,
                env=tool_env,
            )
            if "does not exist yet" not in preflight.stdout:
                registry = "TestPyPI" if "test.pypi.org" in api_base else "PyPI"
                raise GateError(
                    f"{registry} already has exact or partial files for {tag}; "
                    "resume the original workflow run instead of starting a new one"
                )
    return "readiness, reviews, tests, reproducible build, Twine, TestPyPI, and PyPI preflights passed"


def _surface_inventory(repo: Path) -> str:
    required = {
        "repository hygiene": ("scripts/check_repo_hygiene.py", ".gitignore", ".gitattributes"),
        "README and hosted documentation": (
            "README.md",
            "mkdocs.yml",
            "docs/index.md",
            "docs/demo.md",
            "docs/case-study-range-review.md",
            "docs/comparison.md",
            "docs/distribution.md",
            "docs/RELEASING.md",
            "docs/assets/verify-demo.svg",
            "docs/assets/logo.svg",
            "docs/assets/logo.png",
            "docs/assets/social-preview.svg",
            "docs/assets/social-preview.png",
            ".github/workflows/docs.yml",
            "scripts/requirements/docs.lock",
            "scripts/demo_range_review.py",
            "examples/range-review/fixture/boundary.config.json",
        ),
        "changelog and release notes": ("CHANGELOG.md", "scripts/release_changelog.py"),
        "schema URLs, configs, and locks": (
            "boundary.config.schema.json",
            "spec/boundary.lock.schema.json",
            "spec/cli-output.review.schema.json",
            "spec/cli-output.plan.schema.json",
            "boundary.lock.json",
        ),
        "CI and review state": (".github/workflows/ci.yml", "scripts/audit_release_reviews.sh"),
        "reproducible wheel, sdist, and standalone archive": (
            "scripts/verify_release_candidate.py",
            "scripts/packaging_smoke.sh",
            "scripts/build_release_artifacts.py",
            "scripts/install_locked_tools.py",
            "scripts/lock_release_tools.py",
            "scripts/release-tool-lock.toml",
            "scripts/release-tool-artifacts.json",
            "scripts/requirements/action.lock",
            "scripts/requirements/ci.lock",
            "scripts/requirements/release.lock",
        ),
        "GitHub Action and Marketplace": (
            "action.yml",
            "scripts/export_action_outputs.py",
            ".github/workflows/publish.yml",
        ),
        "TestPyPI": ("scripts/verify_testpypi_release.py",),
        "PyPI": ("scripts/verify_testpypi_release.py",),
        "GitHub Release assets": ("scripts/verify_release_surfaces.py",),
        "compatibility alias": (
            ".github/workflows/publish.yml",
            ALIAS_WORKFLOW_PATH,
            "scripts/release_alias.py",
        ),
        "GHCR multi-platform container": (
            "Dockerfile",
            ".github/workflows/publish-container.yml",
            ".github/workflows/publish.yml",
        ),
        "Homebrew tap": (
            "scripts/render_homebrew_formula.py",
            "docs/distribution.md",
        ),
        "GitLab CI/CD Catalog component": (
            ".gitlab-ci.yml",
            "templates/boundver.yml",
            "scripts/validate_gitlab_component.py",
        ),
        "pre-commit": (".pre-commit-hooks.yaml",),
    }
    missing = [
        f"{surface}: {path}"
        for surface, paths in required.items()
        for path in paths
        if not (repo / path).exists()
    ]
    if missing:
        raise GateError("missing release surface files: " + ", ".join(missing))
    publish_workflow = _read_bounded_text(
        repo / ".github/workflows/publish.yml",
        ".github/workflows/publish.yml",
        max_bytes=MAX_RELEASE_WORKFLOW_BYTES,
    )
    required_jobs = (
        "testpypi-preflight",
        "publish-testpypi",
        "verify-testpypi",
        "prepare-release-notes",
        "prepare-release-draft",
        "verify-marketplace",
        "pypi-preflight",
        "publish-pypi",
        "verify-pypi",
        "publish-container",
        "advance-compatibility-alias",
        "verify-public-surfaces",
    )
    absent_jobs = [name for name in required_jobs if f"  {name}:" not in publish_workflow]
    if absent_jobs:
        raise GateError(
            "publication workflow is missing release phases: " + ", ".join(absent_jobs)
        )
    required_recovery_contracts = (
        "container-artifact-id:",
        "container-artifact-name:",
        "Re-retain the exact recovered OCI image for the protected publisher",
        "reuse_retained_artifact: ${{ needs.verify-release.outputs.container-artifact-id != '' }}",
        "retained_artifact_name: ${{ needs.verify-release.outputs.container-artifact-name }}",
    )
    missing_recovery_contracts = [
        value for value in required_recovery_contracts if value not in publish_workflow
    ]
    if missing_recovery_contracts:
        raise GateError(
            "publication workflow is missing retained-container recovery contracts: "
            + ", ".join(missing_recovery_contracts)
        )
    container_workflow = _read_bounded_text(
        repo / ".github/workflows/publish-container.yml",
        ".github/workflows/publish-container.yml",
        max_bytes=MAX_RELEASE_WORKFLOW_BYTES,
    )
    required_container_contracts = (
        "environment: container",
        "environment: container-public",
        "linux/amd64,linux/arm64",
        "tonistiigi/binfmt:qemu-v10.2.3@sha256:",
        "version: v0.36.1",
        "image=moby/buildkit:v0.32.2@sha256:",
        "push-to-registry: true",
        "oras cp --from-oci-layout",
        '"$archive@$ARCHIVE_DIGEST" "$IMAGE:$version"',
        'DOCKER_CONFIG="$anonymous_config" docker pull',
        'gh attestation verify "oci://$IMAGE@$DIGEST"',
        "reuse_retained_artifact:",
        "retained_artifact_name:",
        "Bind consumers to the artifact-producing attempt",
        "Require the prevalidated retained image in recovery mode",
    )
    missing_container_contracts = [
        value for value in required_container_contracts if value not in container_workflow
    ]
    if missing_container_contracts:
        raise GateError(
            "container workflow is missing release contracts: "
            + ", ".join(missing_container_contracts)
        )
    alias_workflow = _read_bounded_text(
        repo / ALIAS_WORKFLOW_PATH,
        ALIAS_WORKFLOW_PATH,
        max_bytes=MAX_RELEASE_WORKFLOW_BYTES,
    )
    required_alias_contracts = (
        "  advance:",
        "Bind verification authority to the exact release tag",
        "Checkout the reviewed publication-control commit",
        "publication_ref:",
        '--publication-ref "$PUBLICATION_REF"',
        "Require the active originating publication and verified PyPI job",
        "--skip-alias",
        "Require the externally advanced leased compatibility alias",
        'scripts/release_alias.py" require',
        "Verify every public surface after alias handoff confirmation",
    )
    required_publish_alias_contracts = (
        "environment: action-alias",
        "Dispatch exact-tag alias handoff verification",
    )
    missing_publish_alias_contracts = [
        value for value in required_publish_alias_contracts if value not in publish_workflow
    ]
    if missing_publish_alias_contracts:
        raise GateError(
            "publication workflow is missing alias handoff contracts: "
            + ", ".join(missing_publish_alias_contracts)
        )
    missing_alias_contracts = [
        value for value in required_alias_contracts if value not in alias_workflow
    ]
    if missing_alias_contracts:
        raise GateError(
            "alias workflow is missing exact-control release contracts: "
            + ", ".join(missing_alias_contracts)
        )
    return "; ".join(SURFACES)


def _evaluate(repo: Path, remote: str, tag: str) -> tuple[str | None, list[Check]]:
    repo = repo.resolve()
    checks: list[Check] = []
    sha = _head(repo)
    _record(checks, "release surface inventory", lambda: _surface_inventory(repo))
    _record(checks, "repository identity", lambda: _repo_identity(repo, remote))
    _record(checks, "clean repository", lambda: _clean(repo))
    _record(checks, "repository hygiene", lambda: _repository_hygiene(repo))
    _record(checks, "project version", lambda: _project(repo, tag))
    local_ready = all(item.status == "passed" for item in checks)
    if sha is None:
        checks.append(Check("main identity", "failed", "HEAD is not a full commit SHA"))
    elif local_ready:
        _record(checks, "main identity", lambda: _main_identity(repo, remote, sha))
        _record(checks, "remote release state", lambda: _remote_release_state(repo, remote, tag))
        if all(item.status == "passed" for item in checks):
            _record(checks, "GitHub controls", lambda: _github_controls(repo, sha, tag))
        if all(item.status == "passed" for item in checks):
            _record(
                checks,
                "complete release gate",
                lambda: _disposable_gate(repo, remote, sha, tag),
            )
    return sha, checks


def _evaluate_resume(
    repo: Path,
    remote: str,
    tag: str,
    alias: str,
    run_id: int,
    release_sha: str,
) -> tuple[str | None, list[Check]]:
    repo = repo.resolve()
    checks: list[Check] = []
    control_sha = _head(repo)
    _record(checks, "release surface inventory", lambda: _surface_inventory(repo))
    _record(checks, "repository identity", lambda: _repo_identity(repo, remote))
    _record(checks, "clean repository", lambda: _clean(repo))
    _record(checks, "repository hygiene", lambda: _repository_hygiene(repo))
    _record(
        checks,
        "release project version",
        lambda: _project_at_commit(repo, release_sha, tag),
    )
    local_ready = all(item.status == "passed" for item in checks)
    if control_sha is None:
        checks.append(Check("main identity", "failed", "HEAD is not a full commit SHA"))
    elif local_ready:
        _record(
            checks,
            "main identity",
            lambda: _main_identity(repo, remote, control_sha),
        )
        _record(
            checks,
            "existing release tag",
            lambda: _resume_release_state(repo, remote, tag, release_sha),
        )
        _record(
            checks,
            "release commit ancestry",
            lambda: _release_is_on_main(repo, release_sha, control_sha),
        )
        if alias != "none" and all(item.status == "passed" for item in checks):
            _record(
                checks,
                "release alias recovery capability",
                lambda: _release_alias_workflow_at_commit(
                    repo, remote, release_sha, tag, alias
                ),
            )
        if all(item.status == "passed" for item in checks):
            _record(
                checks,
                "GitHub controls",
                lambda: _github_controls(
                    repo, control_sha, tag, allow_resumable_release=True
                ),
            )
        if all(item.status == "passed" for item in checks):
            _record(
                checks,
                "source publication artifacts",
                lambda: _source_release_artifacts(
                    repo, run_id, tag, release_sha, alias
                ),
            )
    return control_sha, checks


def _evaluate_alias(
    repo: Path,
    remote: str,
    tag: str,
    alias: str,
    run_id: int,
    release_sha: str,
) -> tuple[str | None, dict[str, object] | None, list[Check]]:
    """Gate the local compatibility-alias handoff against one active publication."""
    repo = repo.resolve()
    checks: list[Check] = []
    control_sha = _head(repo)
    publication: dict[str, object] | None = None
    _record(checks, "release surface inventory", lambda: _surface_inventory(repo))
    _record(checks, "repository identity", lambda: _repo_identity(repo, remote))
    _record(checks, "clean repository", lambda: _clean(repo))
    _record(checks, "repository hygiene", lambda: _repository_hygiene(repo))
    _record(
        checks,
        "release project version",
        lambda: _project_at_commit(repo, release_sha, tag),
    )
    local_ready = all(item.status == "passed" for item in checks)
    if control_sha is None:
        checks.append(Check("main identity", "failed", "HEAD is not a full commit SHA"))
    elif local_ready:
        _record(
            checks,
            "main identity",
            lambda: _main_identity(repo, remote, control_sha),
        )
        _record(
            checks,
            "existing release tag",
            lambda: _resume_release_state(repo, remote, tag, release_sha),
        )
        _record(
            checks,
            "release commit ancestry",
            lambda: _release_is_on_main(repo, release_sha, control_sha),
        )
        if all(item.status == "passed" for item in checks):
            _record(
                checks,
                "GitHub controls",
                lambda: _github_controls(
                    repo,
                    control_sha,
                    tag,
                    allow_resumable_release=True,
                    expected_active_publication_run_id=run_id,
                ),
            )
        if all(item.status == "passed" for item in checks):
            publication = _record(
                checks,
                "active alias publication",
                lambda: _active_alias_publication(
                    repo, run_id, tag, release_sha, control_sha
                ),
            )
            if isinstance(publication, dict):
                checks[-1] = Check(
                    "active alias publication", "passed", str(publication["detail"])
                )
    if alias != tag.rsplit(".", 1)[0]:
        checks.append(
            Check(
                "compatibility alias policy",
                "failed",
                f"alias must be the release line {tag.rsplit('.', 1)[0]}",
            )
        )
    return control_sha, publication, checks


def _emit(
    args: argparse.Namespace,
    sha: str | None,
    checks: list[Check],
    dispatch: dict[str, str] | None,
) -> int:
    ok = all(item.status == "passed" for item in checks)
    status = "failed"
    if ok:
        if args.command in {"start", "resume"}:
            status = "dispatched"
        elif args.command == "alias":
            status = "advanced"
        else:
            status = "ready"
    payload = {
        "schema_version": 1,
        "phase": args.command,
        "tag": args.tag,
        "sha": sha,
        "status": status,
        "checks": [asdict(item) for item in checks],
        "dispatch": dispatch,
    }
    if args.format == "json":
        print(json.dumps(payload, sort_keys=True))
    else:
        for item in checks:
            marker = "PASS" if item.status == "passed" else "FAIL"
            print(f"[{marker}] {item.name}: {item.detail}")
        if dispatch:
            print(dispatch["detail"])
        print(f"Release {args.command} {'passed' if ok else 'failed'} for {args.tag}.")
    return 0 if ok else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Gated boundver release: check, start, safely resume, or advance the "
            "Action alias."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("check", "start", "resume"):
        child = subparsers.add_parser(command)
        child.add_argument("--tag", required=True, help="Exact vMAJOR.MINOR.PATCH tag")
        child.add_argument("--repo", type=Path, default=Path("."))
        child.add_argument("--remote", default="origin")
        child.add_argument("--format", choices=("text", "json"), default="text")
        if command in {"start", "resume"}:
            child.add_argument("--alias", required=True, help="Explicit vMAJOR.MINOR alias or none")
            confirmation_help = "Exact TAG@40-character-SHA confirmation"
            if command == "resume":
                child.add_argument(
                    "--run-id",
                    required=True,
                    help="Positive decimal ID of the failed original publish run",
                )
                confirmation_help += " followed by #RUNID"
            child.add_argument("--confirm", required=True, help=confirmation_help)
    alias = subparsers.add_parser("alias")
    alias.add_argument("--tag", required=True, help="Exact vMAJOR.MINOR.PATCH tag")
    alias.add_argument("--repo", type=Path, default=Path("."))
    alias.add_argument("--remote", default="origin")
    alias.add_argument("--format", choices=("text", "json"), default="text")
    alias.add_argument("--alias", required=True, help="Exact vMAJOR.MINOR alias")
    alias.add_argument(
        "--run-id", required=True, help="Positive decimal ID of the active publish run"
    )
    alias.add_argument(
        "--confirm",
        required=True,
        help="Exact TAG@40-character-SHA#RUNID confirmation",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if TAG_RE.fullmatch(args.tag) is None:
        parser.error("--tag must be an exact vMAJOR.MINOR.PATCH release")
    confirmation_sha: str | None = None
    if args.command in {"start", "resume", "alias"}:
        expected_alias = args.tag.rsplit(".", 1)[0]
        if args.command == "alias":
            if ALIAS_RE.fullmatch(args.alias) is None or args.alias != expected_alias:
                parser.error(f"--alias must be {expected_alias}")
        elif args.alias != "none" and (
            ALIAS_RE.fullmatch(args.alias) is None or args.alias != expected_alias
        ):
            parser.error(f"--alias must be {expected_alias} or none")
        if args.command == "start":
            confirmation = args.confirm.partition("@")
            if (
                confirmation[0] != args.tag
                or confirmation[1] != "@"
                or SHA_RE.fullmatch(confirmation[2]) is None
            ):
                parser.error("--confirm must be the exact TAG@lowercase-40-character-SHA")
            confirmation_sha = confirmation[2]
        else:
            if RUN_ID_RE.fullmatch(args.run_id) is None:
                parser.error("--run-id must be a positive decimal with no leading zero")
            args.run_id = int(args.run_id)
            if args.run_id > MAX_GITHUB_NUMERIC_ID:
                parser.error("--run-id exceeds the supported GitHub numeric ID range")
            match = re.fullmatch(
                rf"{re.escape(args.tag)}@(?P<sha>[0-9a-f]{{40}})#"
                rf"(?P<run_id>[1-9][0-9]{{0,19}})",
                args.confirm,
            )
            if (
                match is None
                or int(match.group("run_id")) != args.run_id
            ):
                parser.error(
                    "--confirm must be the exact TAG@lowercase-40-character-SHA#RUNID"
                )
            confirmation_sha = match.group("sha")

    control_sha: str | None = None
    if args.command == "resume":
        assert confirmation_sha is not None
        control_sha, checks = _evaluate_resume(
            args.repo,
            args.remote,
            args.tag,
            args.alias,
            args.run_id,
            confirmation_sha,
        )
        sha = confirmation_sha
    elif args.command == "alias":
        assert confirmation_sha is not None
        control_sha, publication, checks = _evaluate_alias(
            args.repo,
            args.remote,
            args.tag,
            args.alias,
            args.run_id,
            confirmation_sha,
        )
        sha = confirmation_sha
    else:
        sha, checks = _evaluate(args.repo, args.remote, args.tag)
    if args.command == "start" and sha != confirmation_sha:
        checks.append(Check("explicit confirmation", "failed", "confirmation SHA does not equal HEAD"))
    if any(item.status == "failed" for item in checks):
        return _emit(args, sha, checks, None)
    if args.command == "check":
        return _emit(args, sha, checks, None)

    if args.command == "alias":
        if publication is None:
            return _emit(args, sha, checks, None)
        assert sha is not None
        assert control_sha is not None
        try:
            # Re-read current main immediately before exposing the maintainer's
            # credential to the reviewed, bounded alias updater.
            _main_identity(args.repo.resolve(), args.remote, control_sha)
            detail = _advance_alias_locally(
                args.repo.resolve(),
                args.tag,
                sha,
                args.alias,
                publication,
            )
        except GateError as error:
            checks.append(Check("compatibility alias advance", "failed", str(error)))
            return _emit(args, sha, checks, None)
        checks.append(Check("compatibility alias advance", "passed", detail))
        return _emit(args, sha, checks, None)

    assert sha is not None
    # Re-read remote main immediately before the command's only mutation.
    try:
        dispatch_control_sha = control_sha if args.command == "resume" else sha
        assert dispatch_control_sha is not None
        _main_identity(args.repo.resolve(), args.remote, dispatch_control_sha)
        workflow = "create-release-tag.yml"
        fields = (
            "--field", f"release_tag={args.tag}",
            "--field", f"release_sha={sha}",
            "--field", f"compatibility_alias={args.alias}",
        )
        if args.command == "resume":
            workflow = "publish.yml"
            fields += ("--field", f"resume_run_id={args.run_id}")
        command = (
            "gh", "workflow", "run", workflow,
            "--repo", REPOSITORY,
            "--ref", "main",
            *fields,
        )
        title = _dispatch_title(
            workflow,
            args.tag,
            sha,
            args.alias,
            args.run_id if args.command == "resume" else None,
        )
        detail = _dispatch_workflow(
            args.repo.resolve(),
            command,
            workflow=workflow,
            control_sha=dispatch_control_sha,
            title=title,
        )
    except GateError as error:
        checks.append(Check("workflow dispatch", "failed", str(error)))
        return _emit(args, sha, checks, None)
    checks.append(Check("workflow dispatch", "passed", detail))
    dispatch = {
        "workflow": workflow,
        "ref": "main",
        "tag": args.tag,
        "sha": sha,
        "control_sha": dispatch_control_sha,
        "alias": args.alias,
        "title": title,
        "detail": detail,
    }
    if args.command == "resume":
        dispatch["resume_run_id"] = str(args.run_id)
    return _emit(args, sha, checks, dispatch)


if __name__ == "__main__":
    raise SystemExit(main())
