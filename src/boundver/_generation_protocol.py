"""Bounded, non-executable worker results; no destination path in the protocol.

The request digest binds a result to controller-owned request bytes, including
captured-source identity. It is not authentication against a trusted custom
provider, nor independent proof that a worker used the declared source.
"""

import hashlib
import json
import struct

from ._lockfile import (
    MAX_LOCKFILE_BYTES,
    _lockfile_structure_issues,
    dump_lockfile,
    parse_lockfile_bytes,
)
from ._lockfile_validation import is_sha256_digest
from ._structured_data import strict_json_loads


MAX_GENERATION_RESULT_HEADER_BYTES = 1024
RESULT_SCHEMA = "boundver-generation-result/v1"


def encode_result(lockfile: dict, *, request_digest: str) -> bytes:
    if not is_sha256_digest(request_digest):
        raise ValueError("Invalid generation request digest")
    if _lockfile_structure_issues(lockfile):
        raise ValueError("Worker result has an invalid lock structure")
    payload = dump_lockfile(lockfile).encode("utf-8")
    header = json.dumps({
        "schema": RESULT_SCHEMA,
        "request_digest": request_digest,
        "payload_digest": hashlib.sha256(payload).hexdigest(),
        "payload_bytes": len(payload),
    }, sort_keys=True, separators=(",", ":")).encode("ascii")
    if len(header) > MAX_GENERATION_RESULT_HEADER_BYTES:
        raise ValueError("Generation result header exceeds its limit")
    return struct.pack(">I", len(header)) + header + payload


def decode_result(data: bytes, *, request_digest: str, running_version=None) -> dict:
    if not is_sha256_digest(request_digest):
        raise ValueError("Invalid generation request digest")
    if not isinstance(data, bytes):
        raise ValueError("Generation result must be bytes")
    if len(data) > 4 + MAX_GENERATION_RESULT_HEADER_BYTES + MAX_LOCKFILE_BYTES:
        raise ValueError("Generation result exceeds its limit")
    if len(data) < 4:
        raise ValueError("Incomplete generation result prefix")
    header_size = struct.unpack(">I", data[:4])[0]
    if not 0 < header_size <= MAX_GENERATION_RESULT_HEADER_BYTES:
        raise ValueError("Invalid generation result header size")
    if len(data) < 4 + header_size:
        raise ValueError("Incomplete generation result header")
    try:
        header = strict_json_loads(data[4:4 + header_size].decode("utf-8"))
    except (ValueError, RecursionError, OverflowError) as exc:
        raise ValueError("Invalid generation result header") from exc
    if not isinstance(header, dict) or set(header) != {
        "schema", "request_digest", "payload_digest", "payload_bytes",
    }:
        raise ValueError("Invalid generation result header fields")
    if header["schema"] != RESULT_SCHEMA or header["request_digest"] != request_digest:
        raise ValueError("Generation result does not match this request")
    size = header["payload_bytes"]
    if type(size) is not int or not 0 < size <= MAX_LOCKFILE_BYTES:
        raise ValueError("Invalid generation result payload size")
    payload = data[4 + header_size:]
    if len(payload) != size:
        raise ValueError("Incomplete or trailing generation result payload")
    if not is_sha256_digest(header["payload_digest"]) or hashlib.sha256(payload).hexdigest() != header["payload_digest"]:
        raise ValueError("Generation result payload digest mismatch")
    lockfile = parse_lockfile_bytes(payload, "generation worker result")
    if _lockfile_structure_issues(lockfile, running_version=running_version):
        raise ValueError("Worker result has an invalid lock structure")
    return lockfile
