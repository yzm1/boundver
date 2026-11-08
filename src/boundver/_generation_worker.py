"""Computation-only worker contract. Publication belongs to the controller.

Trusted native providers retain ordinary Python capabilities; this module is
not a sandbox or an authorization boundary against such providers.
"""

import hashlib
import os
from pathlib import Path
import stat
import sys

from ._bounded_io import read_bounded_file
from ._config import MAX_CONFIG_BYTES, dump_config, validate_config
from ._generation_protocol import MAX_GENERATION_RESULT_HEADER_BYTES, encode_result
from ._generation_snapshot import snapshot_from_value
from ._lockfile import MAX_LOCKFILE_BYTES, dump_lockfile, generate_lockfile, generate_lockfile_for_components
from ._lockfile_validation import is_sha256_digest
from ._structured_data import strict_json_loads
from ._utils import _bounded_diagnostic_text, _bounded_exception_text, _bounded_json_dumps


MAX_GENERATION_REQUEST_BYTES = 64 * 1024 * 1024
MAX_GENERATION_ERROR_BYTES = 8192
REQUEST_SCHEMA = "boundver-generation-request/v1"


def _validate_request(value: object):
    if not isinstance(value, dict) or set(value) != {
        "schema", "operation_id", "repo_root", "config", "source", "snapshot",
        "strict", "allow_custom_providers", "components", "existing_lock", "running_version",
    }:
        raise ValueError("Invalid generation request fields")
    if value["schema"] != REQUEST_SCHEMA or not is_sha256_digest(value["operation_id"]):
        raise ValueError("Invalid generation request identity")
    root = value["repo_root"]
    if not isinstance(root, str) or "\0" in root or not os.path.isabs(root):
        raise ValueError("Generation repository must be an absolute path")
    if type(value["strict"]) is not bool or type(value["allow_custom_providers"]) is not bool:
        raise ValueError("Generation policies must be booleans")
    version = value["running_version"]
    if not isinstance(version, str) or not 0 < len(version) <= 256 or not version.isascii():
        raise ValueError("Invalid generation runtime version")
    config = value["config"]
    if not isinstance(config, dict):
        raise ValueError("Generation config must be an object")
    if len(dump_config(config).encode("utf-8")) > MAX_CONFIG_BYTES:
        raise ValueError("Generation config exceeds its limit")
    source = value["source"]
    if source not in ("head", "index", "working-tree"):
        raise ValueError("Invalid generation source")
    if source == "working-tree":
        if value["snapshot"] is not None:
            raise ValueError("Working-tree generation requires live source inputs")
        snapshot = None
    else:
        snapshot = snapshot_from_value(value["snapshot"])
        if snapshot.source != source:
            raise ValueError("Generation source does not match captured snapshot")
    components = value["components"]
    if components is not None:
        if not isinstance(components, list) or not components or len(components) > len(config.get("components", {})):
            raise ValueError("Invalid generation component selection")
        if not all(isinstance(name, str) and name in config.get("components", {}) for name in components):
            raise ValueError("Unknown generation component selection")
        if len(set(components)) != len(components):
            raise ValueError("Duplicate generation component selection")
        if not isinstance(value["existing_lock"], dict):
            raise ValueError("Scoped worker generation requires a captured base lock")
        dump_lockfile(value["existing_lock"])
    elif value["existing_lock"] is not None:
        raise ValueError("Full worker generation must not receive a base lock")
    return snapshot


def encode_request(value: dict) -> bytes:
    _validate_request(value)
    return _bounded_json_dumps(
        value, allow_nan=False, sort_keys=True,
        separators=(",", ":"), max_bytes=MAX_GENERATION_REQUEST_BYTES,
    ).encode("utf-8")


def compute_request(data: bytes, *, running_version: str) -> bytes:
    """Return a bound result; never write a lock or read a destination file."""
    if not isinstance(data, bytes) or len(data) > MAX_GENERATION_REQUEST_BYTES:
        raise ValueError("Generation request exceeds its byte limit")
    value = strict_json_loads(data.decode("utf-8"))
    snapshot = _validate_request(value)
    if value["running_version"] != running_version:
        raise ValueError("Worker runtime does not match controller runtime")
    root = Path(value["repo_root"])
    errors = validate_config(
        value["config"], root, source=value["source"], snapshot=snapshot,
        allow_custom_providers=value["allow_custom_providers"],
        require_slice_facets=value["strict"],
    )
    if errors:
        first = _bounded_diagnostic_text(errors[0], max_chars=512)
        raise ValueError(f"Worker config validation failed ({len(errors)} issues): {first}")
    arguments = dict(
        source=value["source"], snapshot=snapshot, strict=value["strict"],
        allow_custom_providers=value["allow_custom_providers"],
    )
    if value["components"] is None:
        lock = generate_lockfile(value["config"], root, **arguments)
    else:
        lock = generate_lockfile_for_components(
            value["config"], root, selected_components=value["components"],
            existing_lockfile=value["existing_lock"], running_version=running_version,
            out_path=root / "boundary.lock.json", **arguments,
        )
    return encode_result(lock, request_digest=hashlib.sha256(data).hexdigest())


def _private_directory(directory: Path) -> None:
    if not directory.is_absolute():
        raise ValueError("Worker transfer directory must be absolute")
    identity = directory.lstat()
    if not stat.S_ISDIR(identity.st_mode) or getattr(identity, "st_file_attributes", 0) & 0x400:
        raise ValueError("Worker transfer directory must be a plain directory")


def write_transfer(directory: Path, name: str, data: bytes) -> None:
    """Create a fixed-name file exclusively inside controller-private storage.

    The controller must create the private directory outside the repository.
    This is not a shared/untrusted-directory writer or a publication primitive.
    """
    limits = {
        "request.json": MAX_GENERATION_REQUEST_BYTES,
        "result.bin": 4 + MAX_GENERATION_RESULT_HEADER_BYTES + MAX_LOCKFILE_BYTES,
        "error.json": MAX_GENERATION_ERROR_BYTES,
    }
    if name not in limits or not isinstance(data, bytes) or len(data) > limits[name]:
        raise ValueError("Invalid or oversized worker transfer")
    _private_directory(directory)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(directory / name, flags, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def run_transfer(directory: Path, *, running_version: str) -> int:
    """Consume one private request and return success only after result close."""
    _private_directory(directory)
    try:
        request = read_bounded_file(
            directory / "request.json", MAX_GENERATION_REQUEST_BYTES,
            trusted_root=directory, path_label="generation worker request",
        )
        result = compute_request(request, running_version=running_version)
        write_transfer(directory, "result.bin", result)
    except Exception as exc:
        # No traceback or arbitrary source contents on inherited output handles.
        # Error transport is bounded independently from the successful payload.
        message = _bounded_exception_text(exc, max_chars=1024)
        error = _bounded_json_dumps({"error": message}, max_bytes=MAX_GENERATION_ERROR_BYTES, allow_nan=False).encode("utf-8")
        try:
            write_transfer(directory, "error.json", error)
        except (OSError, ValueError):
            pass
        return 2
    return 0


if __name__ == "__main__":
    from . import __version__

    if len(sys.argv) != 2:
        raise SystemExit(2)
    raise SystemExit(run_transfer(Path(sys.argv[1]), running_version=__version__))
