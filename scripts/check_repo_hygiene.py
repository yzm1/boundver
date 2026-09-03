#!/usr/bin/env python3
"""Validate repository hygiene without changing the worktree or index."""

from __future__ import annotations

import argparse
import importlib.util
import locale
import os
import shutil
import stat
import subprocess
import sys
import threading
from pathlib import Path
from typing import BinaryIO, Sequence


def _load_release_platform():
    """Load the exact adjacent helper even under isolated Python startup."""
    path = Path(__file__).resolve().with_name("_release_platform.py")
    spec = importlib.util.spec_from_file_location(
        "_boundver_hygiene_release_platform", path
    )
    if spec is None or spec.loader is None:  # pragma: no cover - import invariant
        raise RuntimeError(f"cannot load release platform helper: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sanitize_git_environment = _load_release_platform().sanitize_git_environment


GENERATED_PARTS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    ".hypothesis",
    ".cache",
    ".eggs",
    "build",
    "dist",
}
GENERATED_NAMES = {".coverage", "coverage.xml", "MANIFEST"}
GENERATED_SUFFIXES = {".pyc", ".pyo"}
TEXT_SUFFIXES = {
    ".json",
    ".md",
    ".py",
    ".pyi",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
TEXT_NAMES = {
    ".dockerignore",
    ".gitattributes",
    ".gitignore",
    ".pre-commit-hooks.yaml",
    "Dockerfile",
    "LICENSE",
    "MANIFEST.in",
}
MAX_GIT_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_GIT_DIAGNOSTIC_BYTES = 64 * 1024
MAX_TRACKED_ENTRIES = 50_000
MAX_TRACKED_RECORD_BYTES = 16 * 1024 + 512
MAX_TRACKED_PATH_BYTES = 16 * 1024
MAX_TRACKED_TOTAL_PATH_BYTES = 16 * 1024 * 1024
MAX_TEXT_FILE_BYTES = 50 * 1024 * 1024
MAX_TEXT_TOTAL_BYTES = 256 * 1024 * 1024
_STREAM_CHUNK_BYTES = 64 * 1024
MAX_GIT_SECONDS = 120


def _trusted_git(repo: Path) -> str:
    """Resolve host Git without allowing repository-local shadowing."""
    selected = shutil.which("git")
    if selected is None:
        raise RuntimeError("git is required")
    try:
        root = repo.resolve(strict=True)
        raw = Path(os.path.abspath(selected))
        resolved = Path(selected).resolve(strict=True)
        identity = resolved.stat()
    except OSError as error:
        raise RuntimeError("trusted git executable is unavailable") from error
    if (
        not stat.S_ISREG(identity.st_mode)
        or raw == root
        or root in raw.parents
        or resolved == root
        or root in resolved.parents
    ):
        raise RuntimeError("refusing a git executable inside the repository")
    return str(resolved)


def _git_environment() -> dict[str, str]:
    environment = sanitize_git_environment()
    environment.update(
        {
            "GCM_INTERACTIVE": "Never",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_KEY_1": "core.fsmonitor",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_VALUE_0": os.devnull,
            "GIT_CONFIG_VALUE_1": "false",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def _git(repo: Path, *arguments: str) -> bytes:
    """Run Git with bounded, concurrently drained stdout and stderr."""
    command = [
        _trusted_git(repo),
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "core.fsmonitor=false",
        "--no-optional-locks",
        *arguments,
    ]
    try:
        process = subprocess.Popen(
            command,
            cwd=repo,
            env=_git_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as error:
        raise RuntimeError("git is required") from error
    if process.stdout is None or process.stderr is None:
        try:
            process.kill()
        except OSError:
            pass
        raise RuntimeError("git command pipes are unavailable")

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
            args=(process.stdout, "stdout", MAX_GIT_OUTPUT_BYTES),
            daemon=True,
        ),
        threading.Thread(
            target=read_stream,
            args=(process.stderr, "stderr", MAX_GIT_DIAGNOSTIC_BYTES),
            daemon=True,
        ),
    )
    for reader in readers:
        reader.start()
    try:
        returncode = process.wait(timeout=MAX_GIT_SECONDS)
    except subprocess.TimeoutExpired as error:
        terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise RuntimeError("git command timed out") from error
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
        limit = (
            MAX_GIT_OUTPUT_BYTES
            if name == "stdout"
            else MAX_GIT_DIAGNOSTIC_BYTES
        )
        raise RuntimeError(f"git {name} exceeds the {limit}-byte limit")
    if returncode != 0:
        detail_bytes = captured["stderr"] or captured["stdout"]
        encoding = locale.getpreferredencoding(False)
        detail = detail_bytes.decode(encoding, "replace").strip()
        raise RuntimeError(detail or "git command failed")
    return captured["stdout"]


def _tracked_entries(repo: Path) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    raw = _git(repo, "ls-files", "--stage", "-z")
    if raw and not raw.endswith(b"\0"):
        raise RuntimeError("git returned a truncated tracked-file listing")
    total_path_bytes = 0
    start = 0
    while start < len(raw):
        end = raw.find(b"\0", start)
        if end < 0:  # pragma: no cover - guarded by the trailing-NUL check
            raise RuntimeError("git returned a truncated tracked-file listing")
        record = raw[start:end]
        start = end + 1
        if not record:
            continue
        if len(record) > MAX_TRACKED_RECORD_BYTES:
            raise RuntimeError(
                "tracked-file record exceeds the "
                f"{MAX_TRACKED_RECORD_BYTES}-byte limit"
            )
        if len(entries) >= MAX_TRACKED_ENTRIES:
            raise RuntimeError(
                f"tracked-file listing exceeds the {MAX_TRACKED_ENTRIES}-entry limit"
            )
        try:
            metadata, raw_path = record.split(b"\t", 1)
        except ValueError as error:
            raise RuntimeError("git returned a malformed tracked-file entry") from error
        if len(raw_path) > MAX_TRACKED_PATH_BYTES:
            raise RuntimeError(
                f"tracked path exceeds the {MAX_TRACKED_PATH_BYTES}-byte limit"
            )
        total_path_bytes += len(raw_path)
        if total_path_bytes > MAX_TRACKED_TOTAL_PATH_BYTES:
            raise RuntimeError(
                "tracked paths exceed the "
                f"{MAX_TRACKED_TOTAL_PATH_BYTES}-byte aggregate limit"
            )
        fields = metadata.split()
        if len(fields) != 3 or fields[2] != b"0":
            raise RuntimeError("the index contains unresolved or malformed entries")
        path = raw_path.decode("utf-8", "surrogateescape")
        entries.append((fields[0].decode("ascii"), path))
    return entries


def _read_tracked_text_file(path: Path, name: str, total_bytes: int) -> bytes:
    """Read one stable regular text file within file and aggregate ceilings."""
    initial = path.lstat()
    if not stat.S_ISREG(initial.st_mode):
        raise RuntimeError(f"tracked text path is not a regular file: {name}")
    if initial.st_size > MAX_TEXT_FILE_BYTES:
        raise RuntimeError(
            f"tracked text file exceeds the {MAX_TEXT_FILE_BYTES}-byte limit: {name}"
        )
    if total_bytes + initial.st_size > MAX_TEXT_TOTAL_BYTES:
        raise RuntimeError(
            "tracked text files exceed the "
            f"{MAX_TEXT_TOTAL_BYTES}-byte aggregate limit"
        )

    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(opened.st_mode)
            or (initial.st_dev, initial.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise RuntimeError(f"tracked text file changed while opening: {name}")
        content = bytearray()
        while True:
            file_remaining = MAX_TEXT_FILE_BYTES - len(content)
            aggregate_remaining = MAX_TEXT_TOTAL_BYTES - total_bytes - len(content)
            remaining = min(file_remaining, aggregate_remaining)
            chunk = stream.read(min(_STREAM_CHUNK_BYTES, remaining + 1))
            if not chunk:
                break
            if len(chunk) > remaining:
                if len(chunk) > file_remaining:
                    raise RuntimeError(
                        "tracked text file exceeds the "
                        f"{MAX_TEXT_FILE_BYTES}-byte limit: {name}"
                    )
                raise RuntimeError(
                    "tracked text files exceed the "
                    f"{MAX_TEXT_TOTAL_BYTES}-byte aggregate limit"
                )
            content.extend(chunk)
        finished = os.fstat(stream.fileno())
    try:
        current = path.lstat()
    except FileNotFoundError as error:
        raise RuntimeError(f"tracked text file disappeared while reading: {name}") from error
    if (
        not stat.S_ISREG(current.st_mode)
        or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        or opened.st_size != finished.st_size
        or opened.st_mtime_ns != finished.st_mtime_ns
        or finished.st_size != len(content)
        or current.st_size != finished.st_size
        or current.st_mtime_ns != finished.st_mtime_ns
    ):
        raise RuntimeError(f"tracked text file changed while reading: {name}")
    return bytes(content)


def _is_generated(path: Path) -> bool:
    return (
        any(part in GENERATED_PARTS or part.endswith(".egg-info") for part in path.parts)
        or path.name in GENERATED_NAMES
        or path.suffix in GENERATED_SUFFIXES
    )


def _is_text(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SUFFIXES or path.name in TEXT_NAMES


def hygiene_errors(repo: Path) -> list[str]:
    """Return deterministic hygiene violations in the tracked repository tree."""
    repo = repo.resolve()
    top = Path(_git(repo, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if top != repo:
        return ["--repo must be the repository root"]

    errors: list[str] = []
    entries = _tracked_entries(repo)
    folded: dict[str, str] = {}
    text_bytes = 0
    for mode, name in entries:
        relative = Path(name)
        if _is_generated(relative):
            errors.append(f"generated artifact is tracked: {name}")

        key = name.casefold()
        previous = folded.setdefault(key, name)
        if previous != name:
            errors.append(
                f"case-colliding tracked paths are not portable: {previous!r} and {name!r}"
            )

        if mode == "100755" and not (
            len(relative.parts) == 2
            and relative.parts[0] == "scripts"
            and relative.suffix == ".sh"
        ):
            errors.append(f"unexpected executable bit on tracked file: {name}")

        if mode != "120000" and _is_text(relative):
            path = repo / relative
            try:
                content = _read_tracked_text_file(path, name, text_bytes)
            except (OSError, RuntimeError) as error:
                errors.append(f"cannot read tracked text file {name}: {error}")
                continue
            text_bytes += len(content)
            if content.startswith(b"\xef\xbb\xbf"):
                errors.append(f"UTF-8 BOM is not allowed: {name}")
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                errors.append(f"tracked text file is not UTF-8: {name}")
                continue
            if "\r" in text:
                errors.append(f"tracked text file contains CR characters: {name}")
            if content and not content.endswith(b"\n"):
                errors.append(f"tracked text file lacks a final newline: {name}")
            if any(line.endswith((" ", "\t")) for line in text.splitlines()):
                errors.append(f"tracked text file has trailing whitespace: {name}")
            if any(
                line.startswith(("<<<<<<< ", ">>>>>>> "))
                for line in text.splitlines()
            ):
                errors.append(f"tracked text file contains conflict markers: {name}")

    return sorted(set(errors))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check tracked files for generated debris and portability hazards."
    )
    parser.add_argument("--repo", type=Path, default=Path("."))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        errors = hygiene_errors(args.repo)
    except (OSError, RuntimeError, UnicodeError, ValueError) as error:
        print(f"Repository hygiene check failed: {error}", file=sys.stderr)
        return 1
    if errors:
        print(f"Repository hygiene check failed ({len(errors)} issue(s)):", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print("Repository hygiene check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
