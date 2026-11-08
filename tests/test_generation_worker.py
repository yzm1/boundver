"""Computation equivalence and no-publication worker contract."""

import copy
import hashlib
import json

import pytest

from boundver._generation_protocol import decode_result
from boundver._generation_snapshot import snapshot_to_value
from boundver._generation_worker import REQUEST_SCHEMA, compute_request, encode_request
from boundver._git import _capture_git_source_snapshot
from boundver._lockfile import generate_lockfile
from boundver.core import _capture_operation_snapshot, _get_version
from tests._repo_fixtures import commit_all, init_git_repo


@pytest.fixture
def invocation(tmp_path):
    init_git_repo(tmp_path)
    config = {"project": "worker", "components": {
        name: {"path": name, "boundary": {"provider": "path-hash", "paths": ["schema.json"]}}
        for name in ("api", "client")
    }}
    for name in config["components"]:
        (tmp_path / name).mkdir()
        (tmp_path / name / "schema.json").write_text("{}", encoding="utf-8")
    (tmp_path / "boundary.config.json").write_text(json.dumps(config), encoding="utf-8")
    commit_all(tmp_path)
    return {
        "schema": REQUEST_SCHEMA, "operation_id": "a" * 64,
        "repo_root": str(tmp_path), "config": config, "source": "head",
        "snapshot": snapshot_to_value(_capture_git_source_snapshot(tmp_path, "head")),
        "strict": True, "allow_custom_providers": False, "components": None,
        "existing_lock": None, "running_version": _get_version(),
    }


@pytest.mark.parametrize("source", ["head", "index", "working-tree"])
def test_worker_matches_direct_generation_and_does_not_publish(invocation, tmp_path, source):
    snapshot = _capture_operation_snapshot(tmp_path, source)
    invocation["source"] = source
    invocation["snapshot"] = snapshot_to_value(snapshot) if snapshot is not None else None
    direct = generate_lockfile(invocation["config"], tmp_path, source=source, snapshot=snapshot)
    sentinel = tmp_path / "boundary.lock.json"
    sentinel.write_bytes(b"prior lock must not be touched")
    data = encode_request(invocation)
    result = compute_request(data, running_version=_get_version())
    assert decode_result(result, request_digest=hashlib.sha256(data).hexdigest()) == direct
    assert sentinel.read_bytes() == b"prior lock must not be touched"


def test_worker_does_not_recapture_moving_head(invocation, tmp_path):
    data = encode_request(invocation)
    old = compute_request(data, running_version=_get_version())
    (tmp_path / "api" / "schema.json").write_text('{"new":true}', encoding="utf-8")
    commit_all(tmp_path)
    assert compute_request(data, running_version=_get_version()) == old


def test_scoped_worker_requires_and_uses_explicit_base_lock(invocation, tmp_path):
    invocation["existing_lock"] = generate_lockfile(invocation["config"], tmp_path)
    invocation["components"] = ["api"]
    data = encode_request(invocation)
    result = decode_result(compute_request(data, running_version=_get_version()), request_digest=hashlib.sha256(data).hexdigest())
    assert result == invocation["existing_lock"]
    assert not (tmp_path / "boundary.lock.json").exists()


@pytest.mark.parametrize("field,value", [
    ("strict", 1), ("allow_custom_providers", "true"), ("repo_root", "relative"),
    ("components", ["missing"]), ("components", ["api"]),
    ("operation_id", "invalid"), ("destination", "boundary.lock.json"),
    ("source", "index"),
])
def test_invalid_invocation_rejected_before_computation(invocation, field, value):
    changed = copy.deepcopy(invocation)
    changed[field] = value
    with pytest.raises(ValueError):
        encode_request(changed)


def test_runtime_mismatch_fails_before_computation(invocation):
    with pytest.raises(ValueError, match="runtime"):
        compute_request(encode_request(invocation), running_version="different")


def test_operation_identity_changes_result_binding(invocation):
    first = encode_request(invocation)
    invocation["operation_id"] = "b" * 64
    second = encode_request(invocation)
    assert first != second
    with pytest.raises(ValueError, match="request"):
        decode_result(compute_request(second, running_version=_get_version()), request_digest=hashlib.sha256(first).hexdigest())


def test_scoped_worker_still_rejects_drift_in_unselected_components(invocation, tmp_path):
    invocation["existing_lock"] = generate_lockfile(invocation["config"], tmp_path)
    invocation["components"] = ["api"]
    (tmp_path / "client" / "schema.json").write_text('{"changed":true}', encoding="utf-8")
    commit_all(tmp_path)
    invocation["snapshot"] = snapshot_to_value(_capture_git_source_snapshot(tmp_path, "head"))
    with pytest.raises(ValueError):
        compute_request(encode_request(invocation), running_version=_get_version())
    assert not (tmp_path / "boundary.lock.json").exists()
