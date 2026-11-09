"""Fail-closed worker handoff, independent of destination publication."""

import hashlib
import json
from pathlib import Path
import struct

import pytest

from boundver._generation_protocol import decode_result, encode_result


REQUEST = "a" * 64


@pytest.fixture
def lockfile():
    return json.loads((Path(__file__).resolve().parents[1] / "boundary.lock.json").read_text(encoding="utf-8"))


def rewrite_header(data, change):
    size = struct.unpack(">I", data[:4])[0]
    header = json.loads(data[4:4 + size])
    change(header)
    new_header = json.dumps(header).encode()
    return struct.pack(">I", len(new_header)) + new_header + data[4 + size:]


def test_roundtrip_preserves_lock_without_adding_protocol_metadata(lockfile):
    assert decode_result(encode_result(lockfile, request_digest=REQUEST), request_digest=REQUEST) == lockfile


@pytest.mark.parametrize("cut", [0, 1, 3, 4, 8, -1])
def test_truncated_result_is_rejected(lockfile, cut):
    data = encode_result(lockfile, request_digest=REQUEST)
    with pytest.raises(ValueError):
        decode_result(data[:cut], request_digest=REQUEST)


@pytest.mark.parametrize("field,value", [
    ("schema", "other"), ("request_digest", "b" * 64),
    ("payload_digest", "b" * 64), ("payload_bytes", True),
    ("payload_bytes", -1), ("payload_bytes", 1000000000),
    ("destination", "boundary.lock.json"),
])
def test_header_changes_cannot_authorize_result(lockfile, field, value):
    data = rewrite_header(encode_result(lockfile, request_digest=REQUEST), lambda header: header.update({field: value}))
    with pytest.raises(ValueError):
        decode_result(data, request_digest=REQUEST)


def test_trailing_bytes_and_corruption_are_rejected(lockfile):
    data = encode_result(lockfile, request_digest=REQUEST)
    for changed in (data + b"x", data[:-1] + b"x"):
        with pytest.raises(ValueError):
            decode_result(changed, request_digest=REQUEST)


def test_valid_checksum_does_not_make_malformed_lock_acceptable(lockfile):
    data = encode_result(lockfile, request_digest=REQUEST)
    size = struct.unpack(">I", data[:4])[0]
    header = json.loads(data[4:4 + size])
    payload = b'{"schema":"boundary-lock/v4"}'
    header.update(payload_bytes=len(payload), payload_digest=hashlib.sha256(payload).hexdigest())
    raw = json.dumps(header).encode()
    with pytest.raises(ValueError, match="lock structure"):
        decode_result(struct.pack(">I", len(raw)) + raw + payload, request_digest=REQUEST)


def test_encoder_rejects_invalid_lock_and_request(lockfile):
    with pytest.raises(ValueError):
        encode_result({}, request_digest=REQUEST)
    with pytest.raises(ValueError):
        encode_result(lockfile, request_digest="not a digest")


def test_duplicate_header_keys_are_rejected(lockfile):
    data = encode_result(lockfile, request_digest=REQUEST)
    size = struct.unpack(">I", data[:4])[0]
    header = data[4:4 + size]
    duplicate = b'{"schema":"untrusted",' + header[1:]
    with pytest.raises(ValueError, match="header"):
        decode_result(struct.pack(">I", len(duplicate)) + duplicate + data[4 + size:], request_digest=REQUEST)


@pytest.mark.parametrize("data", [
    struct.pack(">I", 0), struct.pack(">I", 1025),
    struct.pack(">I", 1) + b"\xff", struct.pack(">I", 2) + b"[]",
])
def test_invalid_header_size_encoding_and_type(data):
    with pytest.raises(ValueError):
        decode_result(data, request_digest=REQUEST)


def test_total_result_limit_is_checked_before_parsing(lockfile, monkeypatch):
    import boundver._generation_protocol as protocol
    data = encode_result(lockfile, request_digest=REQUEST)
    monkeypatch.setattr(protocol, "MAX_LOCKFILE_BYTES", 1)
    with pytest.raises(ValueError, match="exceeds its limit"):
        decode_result(data, request_digest=REQUEST)
